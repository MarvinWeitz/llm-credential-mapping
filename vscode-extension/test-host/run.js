// Opt-in: `npm run test:host` downloads VS Code (cached in .vscode-test/) and runs suite.js in a real extension host
// with a fake llm-credentials CLI and a fake gateway that records request headers.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { runTests } = require("@vscode/test-electron");

const requests = [];

function gateway() {
  return http.createServer((request, response) => {
    let raw = "";
    request.on("data", (chunk) => (raw += chunk));
    request.on("end", () => {
      requests.push({ url: request.url, headers: request.headers, body: raw ? JSON.parse(raw) : undefined });
      if (request.url === "/v1/models") {
        response.writeHead(200, { "Content-Type": "application/json" })
          .end(JSON.stringify({ data: [{ id: "alias" }, { id: "tool-model" }] }));
        return;
      }
      const body = JSON.parse(raw);
      const delta = body.model === "tool-model"
        ? { tool_calls: [{ index: 0, id: "call_1", type: "function", function: { name: "list_files", arguments: '{"path":"."}' } }] }
        : { content: "HOST_OK" };
      response.writeHead(200, { "Content-Type": "text/event-stream" });
      response.end(`data: ${JSON.stringify({ choices: [{ index: 0, delta }] })}\n\n`
        + `data: ${JSON.stringify({ choices: [{ index: 0, delta: {}, finish_reason: "stop" }] })}\n\ndata: [DONE]\n\n`);
    });
  });
}

async function main() {
  // Set when this runs from a VS Code terminal or extension; it would start the test VS Code as plain Node.
  delete process.env.ELECTRON_RUN_AS_NODE;
  const server = gateway();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "llm-credentials-host-"));
  const workspace = path.join(root, "workspace");
  fs.mkdirSync(path.join(workspace, ".vscode"), { recursive: true });
  fs.writeFileSync(path.join(workspace, ".vscode", "settings.json"), JSON.stringify({
    "llmCredentials.command": [process.execPath, path.join(__dirname, "fake-cli.js")],
  }));
  const results = path.join(root, "results.txt");
  try {
    await runTests({
      extensionDevelopmentPath: path.resolve(__dirname, ".."),
      extensionTestsPath: path.join(__dirname, "suite.js"),
      launchArgs: [workspace, "--disable-extensions", "--user-data-dir", path.join(root, "user-data"), "--skip-welcome"],
      extensionTestsEnv: {
        LLM_CREDENTIALS_TEST_GATEWAY: `http://127.0.0.1:${server.address().port}`,
        LLM_CREDENTIALS_TEST_RESULTS: results,
      },
    });
  } finally {
    server.close();
    if (fs.existsSync(results)) process.stdout.write(fs.readFileSync(results, "utf8"));
  }
  const chats = requests.filter((request) => request.url === "/v1/chat/completions");
  assert.equal(chats.length, 2, JSON.stringify(requests.map((request) => request.url)));
  for (const chat of chats) {
    assert.equal(chat.headers.authorization, "Bearer host-token");
    assert.equal(chat.headers["x-test-customer"], "customer:host");
    assert.equal(chat.body.stream, true);
  }
  assert.equal(chats[1].body.tools[0].function.name, "list_files");
  console.log("ok gateway saw the credential, the customer headers and the tools");
  fs.rmSync(root, { recursive: true, force: true });
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
