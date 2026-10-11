// HTTP calls to the gateway's OpenAI-compatible endpoint, without importing vscode.
import { StreamDecoder, type OpenAIMessage, type StreamEvent } from "./protocol";

/** A gateway answer that is not a success; `status` 401 means the credential should be renewed. */
export class GatewayError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function failure(response: Response): Promise<GatewayError> {
  let detail = "";
  try {
    const body = await response.json() as { error?: { message?: unknown } | string };
    const message = typeof body.error === "string" ? body.error : body.error?.message;
    if (typeof message === "string") detail = `: ${message.slice(0, 500)}`;
  } catch {
    // The body is optional detail.
  }
  return new GatewayError(`The gateway answered HTTP ${response.status}${detail}`, response.status);
}

/** Model ids from `<endpoint>/models`. */
export async function listModels(endpoint: string, token: string, signal?: AbortSignal): Promise<string[]> {
  const response = await fetch(`${endpoint}/models`, {
    headers: { Authorization: `Bearer ${token}`, Accept: "application/json" }, redirect: "error", signal,
  });
  if (!response.ok) throw await failure(response);
  const body = await response.json() as { data?: Array<{ id?: unknown }> };
  return [...new Set((body.data ?? []).map((model) => model.id).filter((id): id is string => typeof id === "string"))].sort();
}

export interface ChatRequest {
  model: string;
  messages: OpenAIMessage[];
  [option: string]: unknown;
}

/** Stream `<endpoint>/chat/completions`, reporting text and tool calls as they arrive. */
export async function streamChat(
  endpoint: string, token: string, headers: Record<string, string>, request: ChatRequest,
  report: (event: StreamEvent) => void, signal?: AbortSignal,
): Promise<void> {
  const attribution = Object.fromEntries(Object.entries(headers).filter(([name]) => name.toLowerCase() !== "authorization"));
  const response = await fetch(`${endpoint}/chat/completions`, {
    method: "POST",
    headers: { ...attribution, Authorization: `Bearer ${token}`, "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ ...request, stream: true }),
    redirect: "error",
    signal,
  });
  if (!response.ok || !response.body) throw await failure(response);
  const decoder = new StreamDecoder();
  const text = new TextDecoder();
  for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
    decoder.push(text.decode(chunk, { stream: true })).forEach(report);
  }
  decoder.push(text.decode() + "\n").forEach(report);
  decoder.flush().forEach(report);
}
