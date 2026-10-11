// Runs inside a real VS Code extension host: uses the extension's models through VS Code's language model API.
const assert = require("node:assert/strict");
const vscode = require("vscode");

async function collect(response) {
  const parts = [];
  for await (const part of response.stream) parts.push(part);
  return parts;
}

async function models() {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    const found = await vscode.lm.selectChatModels({ vendor: "llm-credentials" });
    if (found.length) return found;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  return [];
}

exports.run = async function run() {
  const results = [];
  const check = async (name, body) => {
    try {
      await body();
      results.push(`ok ${name}`);
    } catch (error) {
      results.push(`FAIL ${name}: ${error && error.stack || error}`);
    }
  };
  const found = await models();
  await check("models are listed from the gateway", async () => {
    assert.deepEqual(found.map((model) => model.id).sort(), ["alias", "tool-model"]);
    assert.equal(found[0].vendor, "llm-credentials");
  });
  const model = found.find((entry) => entry.id === "alias");
  await check("a chat request streams text with the credential and the customer", async () => {
    const response = await model.sendRequest([vscode.LanguageModelChatMessage.User("Say OK")], {}, new vscode.CancellationTokenSource().token);
    const parts = await collect(response);
    assert.equal(parts.map((part) => part.value).join(""), "HOST_OK");
  });
  const toolModel = found.find((entry) => entry.id === "tool-model");
  await check("tool calls come back as tool call parts", async () => {
    const response = await toolModel.sendRequest(
      [vscode.LanguageModelChatMessage.User("List files")],
      { tools: [{ name: "list_files", description: "List files", inputSchema: { type: "object", properties: { path: { type: "string" } } } }] },
      new vscode.CancellationTokenSource().token,
    );
    const parts = await collect(response);
    const call = parts.find((part) => part instanceof vscode.LanguageModelToolCallPart);
    assert.ok(call, JSON.stringify(parts));
    assert.equal(call.name, "list_files");
    assert.deepEqual(call.input, { path: "." });
  });
  await check("the extension is active", async () => {
    assert.ok(vscode.extensions.getExtension("llm-credentials.llm-credentials-vscode").isActive);
  });
  require("node:fs").writeFileSync(process.env.LLM_CREDENTIALS_TEST_RESULTS, results.join("\n") + "\n");
  if (results.some((line) => line.startsWith("FAIL"))) throw new Error(results.join("\n"));
};
