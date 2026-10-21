// Conversion between VS Code chat messages and the OpenAI Chat Completions wire format, without importing vscode.
// Parts are recognised by shape, so the functions can be tested in plain Node.

export const Role = { User: 1, Assistant: 2, System: 3 } as const;
export const ToolMode = { Auto: 1, Required: 2 } as const;

export interface InputMessage {
  readonly role: number;
  readonly content: ReadonlyArray<unknown>;
}

export interface InputTool {
  readonly name: string;
  readonly description?: string;
  readonly inputSchema?: object;
}

export type OpenAIContent = string | Array<{ type: "text"; text: string } | { type: "image_url"; image_url: { url: string } }>;

export interface OpenAIMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: OpenAIContent | null;
  tool_calls?: Array<{ id: string; type: "function"; function: { name: string; arguments: string } }>;
  tool_call_id?: string;
}

const isObject = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;
const isText = (part: unknown): part is { value: string } =>
  isObject(part) && typeof part.value === "string" && !("callId" in part);
const isToolCall = (part: unknown): part is { callId: string; name: string; input: unknown } =>
  isObject(part) && typeof part.callId === "string" && typeof part.name === "string" && "input" in part;
const isToolResult = (part: unknown): part is { callId: string; content: ReadonlyArray<unknown> } =>
  isObject(part) && typeof part.callId === "string" && Array.isArray(part.content);
const isImage = (part: unknown): part is { mimeType: string; data: Uint8Array } =>
  isObject(part) && typeof part.mimeType === "string" && part.mimeType.startsWith("image/") && part.data instanceof Uint8Array;

function text(parts: ReadonlyArray<unknown>): string {
  return parts.map((part) => (isText(part) ? part.value : "")).join("");
}

/** Convert a VS Code conversation; tool results become `tool` messages right after the assistant's tool calls. */
export function toOpenAIMessages(messages: ReadonlyArray<InputMessage>): OpenAIMessage[] {
  const result: OpenAIMessage[] = [];
  for (const message of messages) {
    if (message.role === Role.Assistant) {
      const calls = message.content.filter(isToolCall).map((part) => ({
        id: part.callId, type: "function" as const,
        function: { name: part.name, arguments: JSON.stringify(part.input ?? {}) },
      }));
      const content = text(message.content);
      result.push({ role: "assistant", content: content || null, ...(calls.length ? { tool_calls: calls } : {}) });
      continue;
    }
    for (const part of message.content.filter(isToolResult)) {
      result.push({ role: "tool", tool_call_id: part.callId, content: text(part.content) });
    }
    const rest = message.content.filter((part) => !isToolResult(part) && !isToolCall(part));
    if (!rest.length) continue;
    const images = rest.filter(isImage);
    const content: OpenAIContent = images.length
      ? [
        ...rest.filter(isText).map((part) => ({ type: "text" as const, text: part.value })),
        ...images.map((part) => ({
          type: "image_url" as const,
          image_url: { url: `data:${part.mimeType};base64,${Buffer.from(part.data).toString("base64")}` },
        })),
      ]
      : text(rest);
    result.push({ role: message.role === Role.System ? "system" : "user", content });
  }
  return result;
}

export interface OpenAITools {
  tools?: Array<{ type: "function"; function: { name: string; description: string; parameters: object } }>;
  tool_choice?: "required";
}

export function toOpenAITools(tools: ReadonlyArray<InputTool> | undefined, toolMode: number): OpenAITools {
  if (!tools?.length) return {};
  return {
    tools: tools.map((tool) => ({
      type: "function" as const,
      function: {
        name: tool.name,
        description: tool.description ?? "",
        parameters: tool.inputSchema ?? { type: "object", properties: {} },
      },
    })),
    ...(toolMode === ToolMode.Required ? { tool_choice: "required" as const } : {}),
  };
}

export type StreamEvent =
  | { kind: "text"; text: string }
  | { kind: "toolCall"; callId: string; name: string; input: object };

/** Decode a streamed Chat Completions response (server-sent events) into text and complete tool calls. */
export class StreamDecoder {
  private buffer = "";
  /** Tool calls in arrival order; `current` maps a stream index to the call it now continues. */
  private readonly calls: Array<{ index: number; id: string; name: string; arguments: string }> = [];
  private readonly current = new Map<number, { index: number; id: string; name: string; arguments: string }>();

  push(chunk: string): StreamEvent[] {
    this.buffer += chunk;
    const lines = this.buffer.split(/\r?\n/);
    this.buffer = lines.pop() ?? "";
    const events: StreamEvent[] = [];
    for (const line of lines) {
      if (!line.startsWith("data:")) continue;
      const data = line.slice(5).trim();
      if (!data || data === "[DONE]") continue;
      let value: any;
      try {
        value = JSON.parse(data);
      } catch {
        throw new Error("The gateway sent an invalid streaming response.");
      }
      if (value?.error) throw new Error(`The gateway reported an error: ${value.error.message ?? "unknown error"}`);
      for (const choice of value?.choices ?? []) {
        const delta = choice?.delta ?? {};
        if (typeof delta.content === "string" && delta.content) events.push({ kind: "text", text: delta.content });
        for (const call of delta.tool_calls ?? []) {
          const index = typeof call.index === "number" ? call.index : 0;
          let entry = this.current.get(index);
          // A new id at a known index is another call: some gateways give every parallel call index 0.
          if (!entry || (call.id && entry.id && call.id !== entry.id)) {
            entry = { index, id: "", name: "", arguments: "" };
            this.calls.push(entry);
            this.current.set(index, entry);
          }
          entry.id ||= call.id ?? "";
          entry.name ||= call.function?.name ?? "";
          entry.arguments += call.function?.arguments ?? "";
        }
        if (choice?.finish_reason) events.push(...this.flush());
      }
    }
    return events;
  }

  /** Emit tool calls still pending when the stream ends. */
  flush(): StreamEvent[] {
    const events: StreamEvent[] = [];
    for (const call of [...this.calls].sort((a, b) => a.index - b.index)) {
      const index = call.index;
      let input: object = {};
      if (call.arguments.trim()) {
        try {
          input = JSON.parse(call.arguments);
        } catch {
          throw new Error(`The model sent invalid arguments for tool ${call.name || index}.`);
        }
      }
      events.push({ kind: "toolCall", callId: call.id || `call_${index}`, name: call.name, input });
    }
    this.calls.length = 0;
    this.current.clear();
    return events;
  }
}

/** About what one full-size image costs upstream; its base64 length says nothing about its tokens. */
export const IMAGE_TOKENS = 1600;

/** A rough token estimate (about four characters per token); the gateway's tokenizer is not available locally. */
export function estimateTokens(value: string | InputMessage): number {
  if (typeof value === "string") return Math.ceil(value.length / 4);
  const images = value.content.filter(isImage).length;
  const rest = { role: value.role, content: value.content.filter((part) => !isImage(part)) };
  return Math.ceil(JSON.stringify(toOpenAIMessages([rest])).length / 4) + images * IMAGE_TOKENS;
}
