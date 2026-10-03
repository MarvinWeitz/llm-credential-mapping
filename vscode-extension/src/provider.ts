import * as vscode from "vscode";
import { createHash } from "node:crypto";
import { Cli, IntegrationError, Profile } from "./cli";
import { sameProfile } from "./claude";
import { convertMessages, discoverModels, Gateway, InputPart, ModelMetadata, ModelOverride } from "./protocol";

export interface ProviderServices {
  binding(): Profile | undefined;
  cli(): Cli;
  overrides(): ModelOverride[];
}

export interface ProviderModel extends vscode.LanguageModelChatInformation {
  profile: Profile;
  metadata: ModelMetadata;
}

function inputPart(part: unknown): InputPart {
  if (part instanceof vscode.LanguageModelTextPart) return { type: "text", value: part.value };
  if (part instanceof vscode.LanguageModelToolCallPart) return { type: "call", id: part.callId, name: part.name, input: part.input };
  if (part instanceof vscode.LanguageModelToolResultPart) return { type: "result", id: part.callId, content: part.content.map(inputPart) };
  if (part instanceof vscode.LanguageModelDataPart) return { type: "image", mime: part.mimeType, data: part.data };
  throw new IntegrationError("Unsupported VS Code message part; text, images and text tool results are supported.");
}

function inputs(messages: readonly vscode.LanguageModelChatRequestMessage[]) {
  return messages.map((message) => {
    if (![vscode.LanguageModelChatMessageRole.User, vscode.LanguageModelChatMessageRole.Assistant].includes(message.role)) {
      throw new IntegrationError("Unsupported VS Code message role.");
    }
    return {
      role: message.role === vscode.LanguageModelChatMessageRole.User ? "user" as const : "assistant" as const,
      ...(message.name ? { name: message.name } : {}), content: message.content.map(inputPart),
    };
  });
}

export class Provider implements vscode.LanguageModelChatProvider<ProviderModel>, vscode.Disposable {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeLanguageModelChatInformation = this.changed.event;
  private cache: { key: string; until: number; models: ProviderModel[] } | undefined;
  private readonly counts = new Map<string, number>();
  constructor(private readonly services: ProviderServices, private readonly gateway = new Gateway()) {}

  refresh(): void { this.cache = undefined; this.counts.clear(); this.changed.fire(); }
  dispose(): void { this.changed.dispose(); }

  private async operation<T>(token: vscode.CancellationToken, timeout: number, action: (signal: AbortSignal) => Promise<T>): Promise<T> {
    const controller = new AbortController();
    const listener = token.onCancellationRequested(() => controller.abort());
    if (token.isCancellationRequested) controller.abort();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(timeout)]);
    try { return await action(signal); }
    catch (error) {
      if (token.isCancellationRequested) throw new vscode.CancellationError();
      if (error instanceof IntegrationError) throw error;
      throw new IntegrationError(signal.aborted ? "LLM operation timed out." : "LLM integration failed; check CLI/gateway configuration.");
    } finally { listener.dispose(); }
  }

  private async connection(expected: Profile, signal: AbortSignal) {
    if (!vscode.workspace.isTrusted) throw new IntegrationError("Trust the workspace before using project credentials.");
    const cli = this.services.cli();
    const profile = await cli.profile(expected.name, signal);
    if (!sameProfile(expected, profile)) throw new IntegrationError("The CLI project mapping changed. Select the project again.");
    return { cli, profile };
  }

  async provideLanguageModelChatInformation(_options: vscode.PrepareLanguageModelChatModelOptions, token: vscode.CancellationToken): Promise<ProviderModel[]> {
    const profile = this.services.binding();
    if (!profile || !vscode.workspace.isTrusted) return [];
    return this.operation(token, 30_000, async (signal) => {
      const manual = this.services.overrides();
      const key = JSON.stringify([profile, manual]);
      const connection = await this.connection(profile, signal);
      if (this.cache?.key === key && this.cache.until > Date.now()) return this.cache.models;
      const credential = await connection.cli.token(profile, signal);
      const [catalog, information] = await Promise.all([
        this.gateway.json(profile, credential, "/v1/models", signal),
        this.gateway.json(profile, credential, "/model/info", signal),
      ]);
      const identity = createHash("sha256").update(JSON.stringify(profile)).digest("hex").slice(0, 16);
      const models = discoverModels(catalog, information, manual, profile.name).map((metadata) => ({
        id: `${profile.name}:${identity}:${encodeURIComponent(metadata.id)}`,
        name: metadata.id, family: metadata.id, version: "1",
        detail: `LiteLLM / ${profile.name}`, tooltip: `${profile.gateway} / team ${profile.team_id}`,
        maxInputTokens: metadata.maxInputTokens, maxOutputTokens: metadata.maxOutputTokens,
        capabilities: { toolCalling: metadata.toolCalling, imageInput: metadata.imageInput },
        profile, metadata,
      }));
      this.cache = { key, until: Date.now() + 300_000, models };
      return models;
    });
  }

  private assertBinding(model: ProviderModel): void {
    const binding = this.services.binding();
    if (!binding || !sameProfile(binding, model.profile)) {
      throw new IntegrationError("Workspace project changed. Start a new chat and select a model for the current project.");
    }
  }

  async provideTokenCount(model: ProviderModel, text: string | vscode.LanguageModelChatRequestMessage, token: vscode.CancellationToken): Promise<number> {
    this.assertBinding(model);
    const messages = convertMessages(inputs(typeof text === "string"
      ? [vscode.LanguageModelChatMessage.User(text)] : [text]), model.metadata);
    const key = model.id + ":" + createHash("sha256").update(JSON.stringify(messages)).digest("hex");
    const cached = this.counts.get(key);
    if (cached !== undefined) return cached;
    return this.operation(token, 30_000, async (signal) => {
      const { cli, profile } = await this.connection(model.profile, signal);
      const count = await this.gateway.count(profile, await cli.token(profile, signal), model.metadata.id, messages, signal);
      if (this.counts.size >= 128) {
        const first = this.counts.keys().next().value;
        if (first !== undefined) this.counts.delete(first);
      }
      this.counts.set(key, count);
      return count;
    });
  }

  async provideLanguageModelChatResponse(
    model: ProviderModel, messages: readonly vscode.LanguageModelChatRequestMessage[],
    options: vscode.ProvideLanguageModelChatResponseOptions, progress: vscode.Progress<vscode.LanguageModelResponsePart>,
    token: vscode.CancellationToken,
  ): Promise<void> {
    this.assertBinding(model);
    const converted = convertMessages(inputs(messages), model.metadata);
    const tools = options.tools?.map((tool) => ({
      type: "function", function: {
        name: tool.name, description: tool.description,
        parameters: tool.inputSchema ?? { type: "object", properties: {} },
      },
    })) ?? [];
    if (tools.length && !model.metadata.toolCalling) throw new IntegrationError("This model does not support tool calling.");
    if (options.toolMode === vscode.LanguageModelChatToolMode.Required && !tools.length) {
      throw new IntegrationError("Required tool mode needs at least one tool.");
    }
    const modelOptions = options.modelOptions ?? {};
    for (const key of Object.keys(modelOptions)) {
      if (!["maxTokens", "temperature"].includes(key)) throw new IntegrationError(`Unsupported model option '${key}'.`);
    }
    const maximum: unknown = modelOptions.maxTokens ?? model.metadata.maxOutputTokens;
    if (typeof maximum !== "number" || !Number.isSafeInteger(maximum) || maximum < 1 || maximum > model.metadata.maxOutputTokens) {
      throw new IntegrationError("maxTokens must be within the gateway model's output limit.");
    }
    const temperature: unknown = modelOptions.temperature;
    if (temperature !== undefined && (typeof temperature !== "number" || !Number.isFinite(temperature) || temperature < 0 || temperature > 2)) {
      throw new IntegrationError("temperature must be between 0 and 2.");
    }
    await this.operation(token, 600_000, async (signal) => {
      // Capture this profile/runtime once. A workspace switch cannot reassign an in-flight request.
      const { cli, profile } = await this.connection(model.profile, signal);
      const credential = await cli.token(profile, signal);
      const count = await this.gateway.count(profile, credential, model.metadata.id, converted, signal, tools);
      if (count > model.metadata.maxInputTokens) throw new IntegrationError("Prompt exceeds the gateway model's input token limit.");
      await this.gateway.complete(profile, credential, {
        model: model.metadata.id, messages: converted, stream: true, max_completion_tokens: maximum,
        ...(temperature !== undefined ? { temperature } : {}),
        ...(tools.length ? { tools, tool_choice: options.toolMode === vscode.LanguageModelChatToolMode.Required ? "required" : "auto" } : {}),
      }, signal, (part) => progress.report(part.type === "text"
        ? new vscode.LanguageModelTextPart(part.value)
        : new vscode.LanguageModelToolCallPart(part.id, part.name, part.input)));
    });
  }
}
