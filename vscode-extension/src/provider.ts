// The Copilot Chat model provider: gateway models, a fresh credential and the workspace's customer per request.
import * as vscode from "vscode";
import { AttributionCache, choose, type Choice } from "./attribution";
import * as cli from "./cli";
import { TokenCache } from "./credentials";
import { GatewayError, listModels, streamChat } from "./gateway";
import { estimateTokens, toOpenAIMessages, toOpenAITools } from "./protocol";

export const VENDOR = "llm-credentials";
const CHAT_API = "openai-chat";
const FORWARDED_OPTIONS = ["temperature", "top_p", "max_tokens", "max_completion_tokens", "stop", "seed"];

export interface Services {
  run: cli.Runner;
  profileName: string;
  tokens: TokenCache;
  customers: AttributionCache;
  /** The folder a request uses when the editor gives no hint; set by the status bar command. */
  chosenFolder(): string | undefined;
  onChoice(choice: Choice): void;
  onError(error: unknown): void;
}

export class Provider implements vscode.LanguageModelChatProvider {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeLanguageModelChatInformation = this.changed.event;
  private endpoint: Promise<string> | undefined;

  constructor(private services: Services) {}

  /** New settings: forget the endpoint and ask VS Code to list the models again. */
  reset(services: Services): void {
    this.services = services;
    this.endpoint = undefined;
    this.changed.fire();
  }

  private chatEndpoint(): Promise<string> {
    this.endpoint ??= cli.profile(this.services.run, this.services.profileName).then((profile) => {
      const url = profile.endpoints?.[CHAT_API];
      if (typeof url !== "string") throw new Error("This profile's gateway offers no OpenAI chat endpoint.");
      return url.replace(/\/+$/, "");
    });
    this.endpoint.catch(() => (this.endpoint = undefined));
    return this.endpoint;
  }

  async provideLanguageModelChatInformation(
    options: vscode.PrepareLanguageModelChatModelOptions, token: vscode.CancellationToken,
  ): Promise<vscode.LanguageModelChatInformation[]> {
    try {
      const endpoint = await this.chatEndpoint();
      const models = await this.withToken((credential) => listModels(endpoint, credential, abortSignal(token)));
      const settings = vscode.workspace.getConfiguration("llmCredentials");
      return models.map((id) => ({
        id, name: id, family: id, version: "1", detail: "llm-credentials",
        tooltip: `${id} through your gateway, attributed to this workspace's customer`,
        maxInputTokens: settings.get<number>("maxInputTokens", 128000),
        maxOutputTokens: settings.get<number>("maxOutputTokens", 16384),
        capabilities: { toolCalling: true, imageInput: settings.get<boolean>("imageInput", false) },
      }));
    } catch (error) {
      if (!options.silent) this.services.onError(error);
      return [];
    }
  }

  async provideLanguageModelChatResponse(
    model: vscode.LanguageModelChatInformation, messages: readonly vscode.LanguageModelChatRequestMessage[],
    options: vscode.ProvideLanguageModelChatResponseOptions, progress: vscode.Progress<vscode.LanguageModelResponsePart>,
    token: vscode.CancellationToken,
  ): Promise<void> {
    try {
      const attribution = await this.attribution();
      const endpoint = await this.chatEndpoint();
      const forwarded = Object.fromEntries(
        Object.entries(options.modelOptions ?? {}).filter(([name]) => FORWARDED_OPTIONS.includes(name)),
      );
      const request = { ...forwarded, model: model.id, messages: toOpenAIMessages(messages), ...toOpenAITools(options.tools, options.toolMode) };
      let reported = false;
      await this.withToken((credential) => streamChat(endpoint, credential, attribution.headers, request, (event) => {
        reported = true;
        progress.report(event.kind === "text"
          ? new vscode.LanguageModelTextPart(event.text)
          : new vscode.LanguageModelToolCallPart(event.callId, event.name, event.input));
      }, abortSignal(token)), () => !reported);
    } catch (error) {
      if (token.isCancellationRequested) return;
      this.services.onError(error);
      throw error instanceof Error ? error : new Error(String(error));
    }
  }

  async provideTokenCount(
    _model: vscode.LanguageModelChatInformation, text: string | vscode.LanguageModelChatRequestMessage,
  ): Promise<number> {
    return estimateTokens(text);
  }

  /** The customer of the folder this request belongs to; fails when none is assigned or the choice is ambiguous. */
  private async attribution(): Promise<cli.CliAttribution> {
    const folders = (vscode.workspace.workspaceFolders ?? []).map((folder) => folder.uri.fsPath);
    const document = vscode.window.activeTextEditor?.document.uri;
    const active = document ? vscode.workspace.getWorkspaceFolder(document)?.uri.fsPath : undefined;
    const choice = await choose(folders, active, this.services.chosenFolder(), this.services.customers);
    this.services.onChoice(choice);
    if (choice.kind === "folder") return choice.attribution;
    if (choice.kind === "none") {
      throw new Error("Open a folder: llm-credentials assigns the customer per workspace folder. Nothing was sent.");
    }
    throw new Error("The workspace folders have different customers; choose one in the status bar. Nothing was sent.");
  }

  /** Call the gateway; after a 401, get a fresh credential and retry once if nothing was streamed yet. */
  private async withToken<T>(call: (credential: string) => Promise<T>, canRetry: () => boolean = () => true): Promise<T> {
    try {
      return await call(await this.services.tokens.get());
    } catch (error) {
      if (!(error instanceof GatewayError) || error.status !== 401 || !canRetry()) throw error;
      this.services.tokens.invalidate();
      return call(await this.services.tokens.get());
    }
  }
}

function abortSignal(token: vscode.CancellationToken): AbortSignal {
  const controller = new AbortController();
  if (token.isCancellationRequested) controller.abort();
  token.onCancellationRequested(() => controller.abort());
  return controller.signal;
}
