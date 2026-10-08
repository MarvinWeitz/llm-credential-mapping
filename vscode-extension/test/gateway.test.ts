import assert from "node:assert/strict";
import { createServer, type IncomingMessage, type Server } from "node:http";
import { after, before, test } from "node:test";
import { GatewayError, listModels, streamChat } from "../src/gateway";
import type { StreamEvent } from "../src/protocol";

let server: Server;
let endpoint: string;
const requests: Array<{ url: string; headers: IncomingMessage["headers"]; body: any }> = [];

before(async () => {
  server = createServer((request, response) => {
    let raw = "";
    request.on("data", (chunk) => (raw += chunk));
    request.on("end", () => {
      requests.push({ url: request.url ?? "", headers: request.headers, body: raw ? JSON.parse(raw) : undefined });
      if (request.headers.authorization !== "Bearer good") {
        response.writeHead(401, { "Content-Type": "application/json" }).end('{"error":{"message":"expired"}}');
      } else if (request.url === "/v1/models") {
        response.writeHead(200, { "Content-Type": "application/json" })
          .end(JSON.stringify({ data: [{ id: "b" }, { id: "a" }, { id: "a" }, { name: "no-id" }] }));
      } else if (request.url === "/v1/redirect/chat/completions") {
        response.writeHead(302, { Location: "/elsewhere" }).end();
      } else {
        response.writeHead(200, { "Content-Type": "text/event-stream" });
        response.write('data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n');
        setTimeout(() => response.end('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'), 10);
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  endpoint = `http://127.0.0.1:${typeof address === "object" && address ? address.port : 0}/v1`;
});

after(() => server.close());

test("models are listed with the credential", async () => {
  assert.deepEqual(await listModels(endpoint, "good"), ["a", "b"]);
  await assert.rejects(listModels(endpoint, "stale"), (error: unknown) => error instanceof GatewayError && error.status === 401);
});

test("chat requests carry the credential and attribution headers and stream text", async () => {
  const events: StreamEvent[] = [];
  await streamChat(endpoint, "good", { "x-test-customer": "acme", authorization: "spoofed" },
    { model: "a", messages: [{ role: "user", content: "Hi" }] }, (event) => events.push(event));
  assert.deepEqual(events, [{ kind: "text", text: "Hi" }]);
  const last = requests[requests.length - 1];
  assert.equal(last.url, "/v1/chat/completions");
  assert.equal(last.headers.authorization, "Bearer good");
  assert.equal(last.headers["x-test-customer"], "acme");
  assert.equal(last.body.stream, true);
  assert.equal(last.body.model, "a");
});

test("errors and redirects are not followed or hidden", async () => {
  await assert.rejects(
    streamChat(endpoint, "stale", {}, { model: "a", messages: [] }, () => undefined),
    (error: unknown) => error instanceof GatewayError && error.status === 401 && /expired/.test(error.message),
  );
  await assert.rejects(streamChat(`${endpoint}/redirect`, "good", {}, { model: "a", messages: [] }, () => undefined));
});
