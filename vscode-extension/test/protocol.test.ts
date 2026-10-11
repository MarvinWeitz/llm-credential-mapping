import assert from "node:assert/strict";
import { test } from "node:test";
import { Role, StreamDecoder, ToolMode, estimateTokens, toOpenAIMessages, toOpenAITools } from "../src/protocol";

const text = (value: string) => ({ value });

test("conversations with tool calls convert to the OpenAI order", () => {
  const messages = toOpenAIMessages([
    { role: Role.System, content: [text("Be brief.")] },
    { role: Role.User, content: [text("List files")] },
    { role: Role.Assistant, content: [text("Calling"), { callId: "call_1", name: "ls", input: { path: "." } }] },
    { role: Role.User, content: [{ callId: "call_1", content: [text("a.txt\n"), text("b.txt")] }, text("Thanks")] },
  ]);
  assert.deepEqual(messages, [
    { role: "system", content: "Be brief." },
    { role: "user", content: "List files" },
    {
      role: "assistant", content: "Calling",
      tool_calls: [{ id: "call_1", type: "function", function: { name: "ls", arguments: '{"path":"."}' } }],
    },
    { role: "tool", tool_call_id: "call_1", content: "a.txt\nb.txt" },
    { role: "user", content: "Thanks" },
  ]);
});

test("assistant messages with only tool calls have null content, and images become data URLs", () => {
  const [assistant, user] = toOpenAIMessages([
    { role: Role.Assistant, content: [{ callId: "c", name: "t", input: {} }] },
    { role: Role.User, content: [text("What is this?"), { mimeType: "image/png", data: new Uint8Array([1, 2, 3]) }] },
  ]);
  assert.equal(assistant.content, null);
  assert.deepEqual(user.content, [
    { type: "text", text: "What is this?" },
    { type: "image_url", image_url: { url: "data:image/png;base64,AQID" } },
  ]);
});

test("tools and the required tool mode are forwarded", () => {
  assert.deepEqual(toOpenAITools([], ToolMode.Auto), {});
  assert.deepEqual(toOpenAITools([{ name: "ls", description: "List", inputSchema: { type: "object" } }], ToolMode.Required), {
    tools: [{ type: "function", function: { name: "ls", description: "List", parameters: { type: "object" } } }],
    tool_choice: "required",
  });
  assert.deepEqual(toOpenAITools([{ name: "x" }], ToolMode.Auto).tools?.[0].function.parameters, { type: "object", properties: {} });
});

test("streamed text and tool calls split across chunks are decoded", () => {
  const decoder = new StreamDecoder();
  const events = [
    'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\ndata: {"choices":[{"delta":{"content":"lo"}}]}\n',
    '\ndata: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_7","function":{"name":"ls","arguments":"{\\"pa"}}]}}]}\n\n',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"th\\":1}"}}]}}]}\r\n\r\n',
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n',
  ].flatMap((chunk) => decoder.push(chunk));
  assert.deepEqual(events, [
    { kind: "text", text: "Hel" },
    { kind: "text", text: "lo" },
    { kind: "toolCall", callId: "call_7", name: "ls", input: { path: 1 } },
  ]);
  assert.deepEqual(decoder.flush(), []);
});

test("stream errors and invalid tool arguments are reported", () => {
  assert.throws(() => new StreamDecoder().push('data: {"error":{"message":"budget exceeded"}}\n'), /budget exceeded/);
  assert.throws(() => new StreamDecoder().push("data: {nope\n"), /invalid streaming/);
  const decoder = new StreamDecoder();
  decoder.push('data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","function":{"name":"t","arguments":"{"}}]}}]}\n');
  assert.throws(() => decoder.flush(), /invalid arguments for tool t/);
});

test("token estimates grow with the content", () => {
  assert.equal(estimateTokens("abcd"), 1);
  assert.ok(estimateTokens({ role: Role.User, content: [text("x".repeat(400))] }) > 100);
});
