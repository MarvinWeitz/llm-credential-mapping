import { strict as assert } from "node:assert";
import { createServer } from "node:http";
import { test } from "node:test";
import { convertMessages, discoverModels, Gateway, ModelMetadata, OutputPart, overrides, readCompletion } from "../src/protocol";

const model: ModelMetadata = { id: "alias", maxInputTokens: 100, maxOutputTokens: 8, toolCalling: true, imageInput: true };
const info = (input = 100) => ({
  model_name: "alias", model_info: { max_input_tokens: input, max_output_tokens: 8, supports_function_calling: true, supports_vision: true },
});

function stream(text: string, split = 3): ReadableStream<Uint8Array> {
  const bytes = Buffer.from(text);
  return new ReadableStream({
    start(controller) {
      for (let index = 0; index < bytes.length; index += split) controller.enqueue(bytes.subarray(index, index + split));
      controller.close();
    },
  });
}

const event = (delta: object, finish_reason: string | null = null) => "data: " + JSON.stringify({ choices: [{ index: 0, delta, finish_reason }] }) + "\r\n\r\n";

test("SSE parser handles byte fragmentation, UTF-8, comments, content and interleaved tool arguments", async () => {
  const output: OutputPart[] = [];
  const text = ": ping\r\n\r\n" + event({ content: "h\u00e9llo" })
    + event({ tool_calls: [{ index: 1, id: "b", type: "function", function: { name: "second", arguments: '{"b":' } }] })
    + event({ tool_calls: [{ index: 0, id: "a", type: "function", function: { name: "first", arguments: '{"a":' } }] })
    + event({ tool_calls: [{ index: 0, function: { arguments: "1}" } }, { index: 1, function: { arguments: "2}" } }] })
    + event({}, "tool_calls") + "data: [DONE]\r\n\r\n";
  await readCompletion(stream(text, 1), new AbortController().signal, (part) => output.push(part));
  assert.deepEqual(output, [
    { type: "text", value: "h\u00e9llo" },
    { type: "call", id: "a", name: "first", input: { a: 1 } },
    { type: "call", id: "b", name: "second", input: { b: 2 } },
  ]);
});

test("partial/error/invalid/tool-truncated streams do not succeed", async () => {
  const cases = [
    event({ content: "partial" }),
    event({}, "stop"),
    'data: {"error":{"message":"secret-body"}}\n\n',
    "data: invalid\n\n",
    event({ tool_calls: [{ index: 0, id: "a", function: { name: "f", arguments: "{" } }] }) + event({}, "tool_calls") + "data: [DONE]\n\n",
    event({}, "length") + "data: [DONE]\n\n",
    "data: [DONE]\n\n",
  ];
  for (const text of cases) {
    await assert.rejects(readCompletion(stream(text), new AbortController().signal, () => {}), (error: Error) => {
      assert.doesNotMatch(error.message, /secret-body/);
      return true;
    });
  }
});

test("SSE bounds oversized events and accepts cancellation", async () => {
  await assert.rejects(readCompletion(stream("data: " + "x".repeat(1_048_577), 1_048_577), new AbortController().signal, () => {}), /size limit/);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(readCompletion(stream(event({}, "stop")), controller.signal, () => {}));
});

test("tool batches validate completely before any call is emitted; required and offered tools are enforced", async () => {
  const output: OutputPart[] = [];
  const body = event({ tool_calls: [
    { index: 0, id: "a", function: { name: "allowed", arguments: "{}" } },
    { index: 1, id: "b", function: { name: "other", arguments: "{}" } },
  ] }) + event({}, "tool_calls") + "data: [DONE]\n\n";
  await assert.rejects(readCompletion(stream(body), new AbortController().signal, (part) => output.push(part), new Set(["allowed"])), /not offered/);
  assert.deepEqual(output, []);
  await assert.rejects(readCompletion(stream(event({}, "stop") + "data: [DONE]\n\n"), new AbortController().signal, () => {}, new Set(["allowed"]), true), /required tool/);
});

test("conversion preserves text, tool calls/results and user images", () => {
  const messages = convertMessages([
    { role: "assistant", content: [{ type: "text", value: "Calling" }, { type: "call", id: "a", name: "read", input: { file: "x" } }] },
    { role: "user", content: [{ type: "result", id: "a", content: [{ type: "text", value: "result" }] }, { type: "text", value: "Next" }, { type: "image", mime: "image/png", data: Uint8Array.from([1, 2]) }] },
  ], model);
  assert.equal(messages[0]?.tool_calls?.[0]?.function.arguments, '{"file":"x"}');
  assert.deepEqual(messages[1], { role: "tool", tool_call_id: "a", content: "result" });
  assert.deepEqual(messages[2]?.content, [{ type: "text", text: "Next" }, { type: "image_url", image_url: { url: "data:image/png;base64,AQI=" } }]);
});

test("unsupported images/results are rejected, not dropped", () => {
  assert.throws(() => convertMessages([{ role: "user", content: [{ type: "image", mime: "image/png", data: new Uint8Array() }] }], { ...model, imageInput: false }), /image/);
  assert.throws(() => convertMessages([{ role: "user", content: [{ type: "result", id: "a", content: [{ type: "image", mime: "image/png", data: new Uint8Array() }] }] }], model), /non-text/);
  assert.throws(() => convertMessages([{ role: "user", content: [{ type: "call", id: "a", name: "f", input: {} }] }], model), /assistant role/);
});

test("discovery intersects allowed aliases, uses conservative deployment limits and explicit overrides", () => {
  const catalog = { data: [{ id: "alias" }] };
  assert.deepEqual(discoverModels(catalog, { data: [info(100), info(80), { model_name: "forbidden", model_info: {} }] }, [], "a"), [{ ...model, maxInputTokens: 80 }]);
  assert.throws(() => discoverModels(catalog, { data: [] }, [], "a"), /Metadata missing/);
  assert.deepEqual(discoverModels(catalog, { data: [] }, [{ ...model, project: "a" }], "a"), [{ ...model, project: "a" }]);
  assert.throws(() => overrides([{ ...model, project: "a", maxInputTokens: 0 }]), /positive token/);
  assert.throws(() => discoverModels(catalog, { data: [] }, [{ ...model, project: "b" }], "a"), /Metadata missing/);
});

test("non-chat models are excluded and unknown capabilities are not advertised", () => {
  const information = { data: [
    { model_name: "embedding", model_info: { mode: "embedding" } },
    { model_name: "alias", model_info: { max_input_tokens: 100, max_output_tokens: 8 } },
  ] };
  const result = discoverModels({ data: [{ id: "embedding" }, { id: "alias" }] }, information, [], "a");
  assert.deepEqual(result, [{ ...model, toolCalling: false, imageInput: false }]);
});

test("gateway refuses redirects without forwarding credentials and redacts HTTP errors", async () => {
  let redirected = false;
  const server = createServer((request, response) => {
    if (request.url === "/redirected") redirected = true;
    response.writeHead(302, { Location: "/redirected" }).end("secret-error-body");
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  const profile = { name: "a", gateway: `http://127.0.0.1:${address.port}`, team_id: "a", allow_http: true };
  try {
    await assert.rejects(new Gateway().json(profile, "synthetic-secret", "/v1/models", AbortSignal.timeout(5000)), (error: Error) => {
      assert.match(error.message, /HTTP 302/);
      assert.doesNotMatch(error.message, /secret/);
      return true;
    });
    assert.equal(redirected, false);
  } finally { server.closeAllConnections(); await new Promise<void>((resolve) => server.close(() => resolve())); }
});
