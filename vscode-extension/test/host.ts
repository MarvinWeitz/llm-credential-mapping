import * as vscode from "vscode";
import { strict as assert } from "node:assert";
import { createServer } from "node:http";
import * as path from "node:path";
import { Cli, Profile, record } from "../src/cli";
import { Provider } from "../src/provider";

export async function run(): Promise<void> {
  const extension = vscode.extensions.getExtension("cbs-consulting.llm-credential-mapping");
  assert.ok(extension);
  await extension.activate();
  const commands = await vscode.commands.getCommands(true);
  for (const command of ["selectProject", "login", "status", "configureClaude", "configureModels"]) {
    assert.ok(commands.includes("llmCredentials." + command));
  }
  assert.deepEqual(await vscode.lm.selectChatModels({ vendor: "cbs-litellm" }), []);
  const requests: { url: string; auth: string | undefined; body: Record<string, unknown> }[] = [];
  let count = 8;
  let switchProject: (() => void) | undefined;
  let cancelRequest: (() => void) | undefined;
  const event = (delta: object, finish_reason: string | null = null) => "data: " + JSON.stringify({ choices: [{ index: 0, delta, finish_reason }] }) + "\n\n";
  const server = createServer((request, response) => {
    let text = "";
    request.on("data", (data: Buffer) => { text += data.toString(); });
    request.on("end", () => {
      const body: Record<string, unknown> = text ? JSON.parse(text) : {};
      requests.push({ url: request.url ?? "", auth: request.headers.authorization, body });
      response.setHeader("Content-Type", "application/json");
      switch (request.url) {
        case "/v1/models": response.end(JSON.stringify({ data: [{ id: "test-chat" }] })); break;
        case "/model/info":
          response.end(JSON.stringify({ data: [{ model_name: "test-chat", model_info: {
            max_input_tokens: 100, max_output_tokens: 8, supports_function_calling: true, supports_vision: true,
          } }] }));
          break;
        case "/utils/token_counter": switchProject?.(); response.end(JSON.stringify({ total_tokens: count })); break;
        case "/v1/chat/completions":
          response.setHeader("Content-Type", "text/event-stream");
          if (cancelRequest) { response.flushHeaders(); cancelRequest(); break; }
          response.end(event({ content: "OK" })
            + event({ tool_calls: [{ index: 0, id: "call-a", type: "function", function: { name: "echo", arguments: '{"value":' } }] })
            + event({ tool_calls: [{ index: 0, function: { arguments: "1}" } }] })
            + event({}, "tool_calls") + "data: [DONE]\n\n");
          break;
        default: response.writeHead(404).end();
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  process.env.LLM_MAPPING_TEST_GATEWAY = `http://127.0.0.1:${address.port}`;
  let binding: Profile = { name: "fixture-a", gateway: process.env.LLM_MAPPING_TEST_GATEWAY, team_id: "team-fixture-a", allow_http: true };
  const original = binding;
  const executable = process.env.LLM_MAPPING_TEST_NODE;
  assert.ok(executable);
  const provider = new Provider({
    binding: () => binding,
    cli: () => new Cli({ executable, args: [path.join(__dirname, "fixture.js")], home: "", dbusAddress: "" }),
    overrides: () => [],
  });
  const cancellation = new vscode.CancellationTokenSource();
  try {
    const config = vscode.workspace.getConfiguration("llmCredentials");
    await config.update("cliExecutable", executable, vscode.ConfigurationTarget.Global);
    await config.update("cliArguments", [path.join(__dirname, "fixture.js")], vscode.ConfigurationTarget.Global);
    let selected = false;
    const selection = vscode.commands.executeCommand("llmCredentials.selectProject").then(() => { selected = true; });
    const deadline = Date.now() + 15_000;
    while (!selected && Date.now() < deadline) {
      await new Promise<void>((resolve) => setTimeout(resolve, 250));
      await vscode.commands.executeCommand("workbench.action.acceptSelectedQuickOpenItem");
    }
    assert.ok(selected, "Project selection must complete in the isolated test window.");
    await selection;
    const registered = (await vscode.lm.selectChatModels({ vendor: "cbs-litellm" }))[0];
    assert.ok(registered, "Selected workspace models must be discoverable through the actual VS Code registry.");
    const response = await registered.sendRequest([vscode.LanguageModelChatMessage.User("hello")], {
      toolMode: vscode.LanguageModelChatToolMode.Required,
      tools: [{ name: "echo", description: "Echo", inputSchema: { type: "object", properties: { value: { type: "number" } } } }],
    }, cancellation.token);
    const registeredOutput: vscode.LanguageModelResponsePart[] = [];
    for await (const part of response.stream) {
      assert.ok(part instanceof vscode.LanguageModelTextPart || part instanceof vscode.LanguageModelToolCallPart);
      registeredOutput.push(part);
    }
    assert.ok(registeredOutput[0] instanceof vscode.LanguageModelTextPart);
    assert.ok(registeredOutput[1] instanceof vscode.LanguageModelToolCallPart);
    const models = await provider.provideLanguageModelChatInformation({ silent: true }, cancellation.token);
    const model = models[0];
    assert.ok(model);
    assert.equal(model.maxInputTokens, 100);
    assert.equal(model.capabilities.toolCalling, true);
    assert.equal(await provider.provideTokenCount(model, "hello", cancellation.token), 8);
    const counted = requests.length;
    assert.equal(await provider.provideTokenCount(model, "hello", cancellation.token), 8);
    assert.equal(requests.length, counted);
    const output: vscode.LanguageModelResponsePart[] = [];
    const messages = [
      vscode.LanguageModelChatMessage.User("hello"),
      vscode.LanguageModelChatMessage.Assistant([new vscode.LanguageModelToolCallPart("previous", "echo", { value: 0 })]),
      vscode.LanguageModelChatMessage.User([new vscode.LanguageModelToolResultPart("previous", [new vscode.LanguageModelTextPart("result")])]),
    ];
    const options = {
      toolMode: vscode.LanguageModelChatToolMode.Required,
      tools: [{ name: "echo", description: "Echo", inputSchema: { type: "object", properties: { value: { type: "number" } } } }],
    };
    count = 100;
    switchProject = () => { binding = { ...original, name: "fixture-b", team_id: "team-fixture-b" }; };
    await provider.provideLanguageModelChatResponse(model, messages, options, { report: (part) => output.push(part) }, cancellation.token);
    assert.ok(output[0] instanceof vscode.LanguageModelTextPart);
    assert.ok(output[1] instanceof vscode.LanguageModelToolCallPart);
    assert.deepEqual(output[1].input, { value: 1 });
    const completion = requests.filter((item) => item.url === "/v1/chat/completions").at(-1);
    assert.ok(completion);
    assert.equal(completion.auth, "Bearer synthetic-token-fixture-a");
    assert.equal(completion.body.tool_choice, "required");
    assert.equal(completion.body.max_completion_tokens, 8);
    assert.doesNotMatch(JSON.stringify(completion.body), /synthetic-token/);
    const last: unknown = Array.isArray(completion.body.messages) ? completion.body.messages.at(-1) : undefined;
    assert.ok(record(last));
    assert.equal(last.role, "tool");
    await assert.rejects(provider.provideTokenCount(model, "next", cancellation.token), /Workspace project changed/);
    const second = (await provider.provideLanguageModelChatInformation({ silent: true }, cancellation.token))[0];
    assert.ok(second);
    assert.notEqual(second.id, model.id);
    switchProject = undefined;
    binding = original;
    count = 101;
    const before = requests.filter((item) => item.url === "/v1/chat/completions").length;
    await assert.rejects(provider.provideLanguageModelChatResponse(model, messages, options, { report: () => {} }, cancellation.token), /input token limit/);
    assert.equal(requests.filter((item) => item.url === "/v1/chat/completions").length, before);
    count = 8;
    const cancel = new vscode.CancellationTokenSource();
    cancelRequest = () => cancel.cancel();
    try {
      await assert.rejects(provider.provideLanguageModelChatResponse(model, messages, options, { report: () => {} }, cancel.token), (error: Error) => error instanceof vscode.CancellationError);
    } finally { cancel.dispose(); }
    console.log("Extension-host smoke passed: actual model-picker registration/sendRequest, metadata, token counts, tool roundtrip, exact 100/101 token boundary, project pinning and cancellation.");
  } finally {
    cancellation.dispose();
    provider.dispose();
    delete process.env.LLM_MAPPING_TEST_GATEWAY;
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}
