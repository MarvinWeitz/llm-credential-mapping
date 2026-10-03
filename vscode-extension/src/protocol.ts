import { IntegrationError, Profile, record } from "./cli";

export interface ModelMetadata {
  id: string;
  maxInputTokens: number;
  maxOutputTokens: number;
  toolCalling: boolean;
  imageInput: boolean;
}

export interface ModelOverride extends ModelMetadata { project: string }

export type ContentPart = { type: "text"; text: string }
  | { type: "image_url"; image_url: { url: string } };
export interface ToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}
export interface Message {
  role: "user" | "assistant" | "tool";
  content: string | ContentPart[] | null;
  name?: string;
  tool_call_id?: string;
  tool_calls?: ToolCall[];
}
export type InputPart = { type: "text"; value: string }
  | { type: "image"; mime: string; data: Uint8Array }
  | { type: "call"; id: string; name: string; input: object }
  | { type: "result"; id: string; content: InputPart[] };
export interface InputMessage { role: "user" | "assistant"; name?: string; content: InputPart[] }
export type OutputPart = { type: "text"; value: string }
  | { type: "call"; id: string; name: string; input: Record<string, unknown> };

export function convertMessages(input: readonly InputMessage[], model: ModelMetadata): Message[] {
  const messages: Message[] = [];
  for (const message of input) {
    let content: ContentPart[] = [];
    let calls: ToolCall[] = [];
    const flush = () => {
      if (!content.length && !calls.length) return;
      messages.push({
        role: message.role, ...(message.name ? { name: message.name } : {}),
        content: content.length ? content : null,
        ...(calls.length ? { tool_calls: calls } : {}),
      });
      content = [];
      calls = [];
    };
    for (const part of message.content) {
      switch (part.type) {
        case "text": content.push({ type: "text", text: part.value }); break;
        case "image":
          if (!model.imageInput || message.role !== "user" || !/^image\/(png|jpeg|webp|gif)$/.test(part.mime)) {
            throw new IntegrationError("This model/message does not support the supplied image format.");
          }
          content.push({ type: "image_url", image_url: { url: `data:${part.mime};base64,${Buffer.from(part.data).toString("base64")}` } });
          break;
        case "call":
          if (!model.toolCalling || message.role !== "assistant") {
            throw new IntegrationError("Tool calls require a tool-capable model and assistant role.");
          }
          calls.push({ id: part.id, type: "function", function: { name: part.name, arguments: JSON.stringify(part.input) } });
          break;
        case "result":
          if (!model.toolCalling || message.role !== "user" || part.content.some((item) => item.type !== "text")) {
            throw new IntegrationError("Only text tool results are supported; non-text results cannot be silently discarded.");
          }
          flush();
          messages.push({
            role: "tool", tool_call_id: part.id,
            content: part.content.map((item) => item.type === "text" ? item.value : "").join(""),
          });
          break;
      }
    }
    flush();
  }
  return messages;
}

function positive(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

export function overrides(value: unknown): ModelOverride[] {
  if (!Array.isArray(value)) throw new IntegrationError("Model overrides must be an array.");
  return value.map((item: unknown) => {
    if (!record(item) || typeof item.project !== "string" || typeof item.id !== "string" || !item.id
      || !positive(item.maxInputTokens) || !positive(item.maxOutputTokens)
      || typeof item.toolCalling !== "boolean" || typeof item.imageInput !== "boolean") {
      throw new IntegrationError("Each model override requires a project, alias, positive token limits and explicit capabilities.");
    }
    return {
      project: item.project, id: item.id, maxInputTokens: item.maxInputTokens,
      maxOutputTokens: item.maxOutputTokens, toolCalling: item.toolCalling, imageInput: item.imageInput,
    };
  });
}

export function discoverModels(catalog: unknown, information: unknown, manual: ModelOverride[], project: string): ModelMetadata[] {
  if (!record(catalog) || !Array.isArray(catalog.data) || !record(information) || !Array.isArray(information.data)) {
    throw new IntegrationError("Gateway returned invalid model discovery data.");
  }
  const aliases = new Set<string>();
  for (const item of catalog.data) {
    if (!record(item) || typeof item.id !== "string" || !item.id) throw new IntegrationError("Gateway returned an invalid model alias.");
    aliases.add(item.id);
  }
  const result: ModelMetadata[] = [];
  for (const id of aliases) {
    const deployments = information.data.filter((item: unknown) => record(item) && item.model_name === id);
    const infos = deployments.map((item: unknown) => record(item) && record(item.model_info) ? item.model_info : undefined);
    if (infos.length && infos.every((info) => typeof info?.mode === "string" && info.mode !== "chat")) continue;
    const configured = manual.find((item) => item.project === project && item.id === id);
    if (configured) {
      result.push(configured);
      continue;
    }
    if (!infos.length || infos.some((info) => !info || !positive(info.max_input_tokens)
      || !positive(info.max_output_tokens))) {
      throw new IntegrationError(`Metadata missing for alias '${id}'. Use LLM: Configure Copilot Models; token limits and capabilities are not guessed.`);
    }
    const limits = infos.map((info) => {
      if (!info || !positive(info.max_input_tokens) || !positive(info.max_output_tokens)) {
        throw new IntegrationError("Invalid gateway model token limits.");
      }
      return { input: info.max_input_tokens, output: info.max_output_tokens };
    });
    result.push({
      id, maxInputTokens: Math.min(...limits.map((item) => item.input)),
      maxOutputTokens: Math.min(...limits.map((item) => item.output)),
      toolCalling: infos.every((info) => info?.supports_function_calling === true),
      imageInput: infos.every((info) => info?.supports_vision === true),
    });
  }
  return result;
}

async function* events(body: ReadableStream<Uint8Array>, signal: AbortSignal): AsyncGenerator<string> {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  let data: string[] = [];
  try {
    while (true) {
      signal.throwIfAborted();
      const chunk = await reader.read();
      buffer += chunk.done ? decoder.decode() : decoder.decode(chunk.value, { stream: true });
      if (buffer.length > 1_048_576) throw new IntegrationError("Gateway SSE event exceeded its size limit.");
      let newline: number;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline).replace(/\r$/, "");
        buffer = buffer.slice(newline + 1);
        if (!line) {
          if (data.length) yield data.join("\n");
          data = [];
        } else if (line.startsWith("data:")) {
          data.push(line.slice(5).replace(/^ /, ""));
          if (data.reduce((sum, item) => sum + item.length, 0) > 1_048_576) {
            throw new IntegrationError("Gateway SSE event exceeded its size limit.");
          }
        }
      }
      if (chunk.done) {
        if (buffer || data.length) throw new IntegrationError("Gateway stream ended in a partial SSE event.");
        return;
      }
    }
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
}

export async function readCompletion(
  body: ReadableStream<Uint8Array>, signal: AbortSignal, emit: (part: OutputPart) => void,
  allowedTools?: ReadonlySet<string>, requiredTool = false,
): Promise<void> {
  const calls = new Map<number, { id: string; name: string; arguments: string }>();
  let finished = false;
  let done = false;
  let total = 0;
  for await (const event of events(body, signal)) {
    if (event === "[DONE]") { done = true; break; }
    let value: unknown;
    try { value = JSON.parse(event); }
    catch { throw new IntegrationError("Gateway returned invalid streaming JSON."); }
    if (!record(value) || value.error) throw new IntegrationError("Gateway reported a streaming error; inspect its logs.");
    if (!Array.isArray(value.choices)) throw new IntegrationError("Gateway stream is missing choices.");
    for (const choice of value.choices) {
      if (!record(choice) || choice.index !== 0) throw new IntegrationError("Only one streamed completion is supported.");
      if (choice.finish_reason === "length" || choice.finish_reason === "content_filter") {
        throw new IntegrationError("Gateway stopped the response at a token limit or content filter.");
      }
      if (choice.finish_reason != null) {
        if (!["stop", "tool_calls"].includes(String(choice.finish_reason))) throw new IntegrationError("Unsupported gateway finish reason.");
        finished = true;
      }
      if (!record(choice.delta)) throw new IntegrationError("Gateway returned an invalid stream delta.");
      for (const field of ["content", "refusal"]) {
        const text = choice.delta[field];
        if (text != null && typeof text !== "string") throw new IntegrationError("Unsupported streamed content.");
        if (typeof text === "string" && text) {
          total += text.length;
          emit({ type: "text", value: text });
        }
      }
      if (choice.delta.tool_calls != null) {
        if (!Array.isArray(choice.delta.tool_calls)) throw new IntegrationError("Invalid streamed tool calls.");
        for (const item of choice.delta.tool_calls) {
          if (!record(item) || typeof item.index !== "number" || !Number.isInteger(item.index) || item.index < 0 || item.index >= 128) {
            throw new IntegrationError("Invalid streamed tool-call index.");
          }
          const call = calls.get(item.index) ?? { id: "", name: "", arguments: "" };
          if (item.id != null) {
            if (typeof item.id !== "string" || (call.id && call.id !== item.id)) throw new IntegrationError("Invalid streamed tool-call ID.");
            call.id = item.id;
          }
          if (item.type != null && item.type !== "function") throw new IntegrationError("Unsupported streamed tool type.");
          if (item.function != null) {
            if (!record(item.function)) throw new IntegrationError("Invalid streamed function.");
            for (const field of ["name", "arguments"] as const) {
              const text = item.function[field];
              if (text != null) {
                if (typeof text !== "string") throw new IntegrationError("Invalid streamed function fragment.");
                call[field] += text;
                total += text.length;
              }
            }
          }
          calls.set(item.index, call);
        }
      }
      if (total > 16_777_216) throw new IntegrationError("Gateway completion exceeded the response size limit.");
    }
  }
  if (!done || !finished) throw new IntegrationError("Gateway stream ended before a complete response.");
  if (requiredTool && !calls.size) throw new IntegrationError("Gateway did not respect required tool mode.");
  const ids = new Set<string>();
  const completed: OutputPart[] = [];
  for (const [, call] of [...calls].sort(([left], [right]) => left - right)) {
    let input: unknown;
    try { input = JSON.parse(call.arguments); }
    catch { throw new IntegrationError("Gateway returned incomplete tool-call arguments."); }
    if (!call.id || !call.name || !record(input) || ids.has(call.id)) throw new IntegrationError("Gateway returned an invalid tool call.");
    if (allowedTools && !allowedTools.has(call.name)) throw new IntegrationError("Gateway requested a tool that was not offered.");
    ids.add(call.id);
    completed.push({ type: "call", id: call.id, name: call.name, input });
  }
  for (const call of completed) emit(call);
}

export class Gateway {
  private async request(profile: Profile, credential: string, path: string, signal: AbortSignal, payload?: object, streaming = false): Promise<Response> {
    try {
      const response = await fetch(profile.gateway + path, {
        method: payload ? "POST" : "GET", redirect: "manual", signal,
        headers: {
          Authorization: `Bearer ${credential}`, Accept: streaming ? "text/event-stream" : "application/json",
          ...(payload ? { "Content-Type": "application/json" } : {}),
        },
        ...(payload ? { body: JSON.stringify(payload) } : {}),
      });
      if (!response.ok) {
        await response.body?.cancel();
        throw new IntegrationError(`Gateway returned HTTP ${response.status}; check login, model access and budgets. Redirects are refused.`);
      }
      return response;
    } catch (error) {
      if (error instanceof IntegrationError) throw error;
      throw new IntegrationError(signal.aborted ? "Gateway request cancelled or timed out." : "Gateway connection failed; check connectivity and trusted TLS certificates.");
    }
  }

  async json(profile: Profile, credential: string, path: string, signal: AbortSignal, payload?: object): Promise<unknown> {
    const response = await this.request(profile, credential, path, signal, payload);
    if (!response.body) throw new IntegrationError("Gateway returned an empty response.");
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let bytes = 0;
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        bytes += chunk.value.length;
        if (bytes > 1_048_576) throw new IntegrationError("Gateway JSON exceeded its size limit.");
        chunks.push(chunk.value);
      }
      let value: unknown;
      try { value = JSON.parse(Buffer.concat(chunks).toString("utf8")); }
      catch { throw new IntegrationError("Gateway returned invalid JSON."); }
      return value;
    } finally {
      await reader.cancel();
      reader.releaseLock();
    }
  }

  async count(profile: Profile, credential: string, model: string, messages: Message[], signal: AbortSignal, tools: object[] = []): Promise<number> {
    const value = await this.json(profile, credential, "/utils/token_counter", signal, { model, messages, ...(tools.length ? { tools } : {}) });
    if (!record(value) || value.error || (typeof value.status_code === "number" && value.status_code >= 400)
      || typeof value.total_tokens !== "number" || !Number.isSafeInteger(value.total_tokens) || value.total_tokens < 0) {
      throw new IntegrationError("Gateway token counter failed. This provider requires /utils/token_counter.");
    }
    return value.total_tokens;
  }

  async complete(profile: Profile, credential: string, payload: object, signal: AbortSignal, emit: (part: OutputPart) => void): Promise<void> {
    const response = await this.request(profile, credential, "/v1/chat/completions", signal, payload, true);
    if (!response.body || !response.headers.get("content-type")?.startsWith("text/event-stream")) {
      await response.body?.cancel();
      throw new IntegrationError("Gateway must return a Chat Completions SSE stream.");
    }
    const tools = record(payload) && Array.isArray(payload.tools) ? payload.tools : [];
    const allowed = new Set<string>();
    for (const tool of tools) {
      if (!record(tool) || !record(tool.function) || typeof tool.function.name !== "string") {
        await response.body.cancel();
        throw new IntegrationError("Invalid request tool definition.");
      }
      allowed.add(tool.function.name);
    }
    try { await readCompletion(response.body, signal, emit, allowed, record(payload) && payload.tool_choice === "required"); }
    catch (error) {
      if (error instanceof IntegrationError) throw error;
      throw new IntegrationError(signal.aborted ? "Gateway request cancelled or timed out." : "Gateway stream was interrupted; the request is not automatically retried.");
    }
  }
}
