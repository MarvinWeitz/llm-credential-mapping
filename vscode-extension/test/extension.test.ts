// activate() with a stand-in `vscode` module: what the extension does after a sign-in, a new profile, a 401 or
// missing labels.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import Module from "node:module";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { GatewayError } from "../src/gateway";

type Listener = (value: any) => unknown;

/** A minimal vscode API; `fire` triggers a registered event, `commands` holds the registered command handlers. */
function fakeVscode(
  remoteName: string | undefined, command: string[], executed: Record<string, () => unknown>, folders: string[] = [],
) {
  const listeners: Record<string, Listener[]> = {};
  const event = (name: string) => (listener: Listener) => {
    (listeners[name] ??= []).push(listener);
    return { dispose() {} };
  };
  const api = {
    warnings: [] as Array<{ message: string; actions: string[] }>,
    terminals: [] as Array<{ name: string; shellArgs: string[] }>,
    /** The items of each quick pick shown; none is picked. */
    picks: [] as Array<Array<{ label: string; description: string; detail: string }>>,
    status: {} as { text?: string; tooltip?: string; backgroundColor?: unknown },
    /** The button the user clicks in the next warning. */
    action: undefined as string | undefined,
    commands: {} as Record<string, (...args: any[]) => unknown>,
    provider: undefined as any,
    fire: (name: string, value?: unknown) => (listeners[name] ?? []).forEach((listener) => listener(value)),
  };
  const vscode = {
    EventEmitter: class {
      private readonly listeners: Listener[] = [];
      event = (listener: Listener) => (this.listeners.push(listener), { dispose() {} });
      fire(value?: unknown) {
        this.listeners.forEach((listener) => listener(value));
      }
    },
    ThemeColor: class {},
    StatusBarAlignment: { Right: 2 },
    env: { remoteName },
    window: {
      createOutputChannel: () => ({ appendLine() {}, dispose() {} }),
      createStatusBarItem: () => Object.assign(api.status, { show() {}, dispose() {} }),
      createTerminal: (options: { name: string; shellArgs: string[] }) => {
        const terminal = { name: options.name, shellArgs: options.shellArgs, show() {} };
        api.terminals.push(terminal);
        return terminal;
      },
      showWarningMessage: async (message: string, ...actions: string[]) => {
        api.warnings.push({ message, actions });
        return actions.includes(api.action as string) ? api.action : undefined;
      },
      showQuickPick: async (items: Array<{ label: string; description: string; detail: string }>) => (api.picks.push(items), undefined),
      onDidCloseTerminal: event("closeTerminal"),
      onDidChangeActiveTextEditor: event("activeEditor"),
      activeTextEditor: undefined,
    },
    workspace: {
      workspaceFolders: folders.map((folder) => ({ name: path.basename(folder), uri: { fsPath: folder } })),
      getConfiguration: () => ({ get: (name: string, fallback: unknown) => (name === "command" ? command : fallback) }),
      onDidChangeConfiguration: event("configuration"),
      onDidChangeWorkspaceFolders: event("folders"),
    },
    commands: {
      registerCommand: (name: string, handler: (...args: any[]) => unknown) => ((api.commands[name] = handler), { dispose() {} }),
      executeCommand: async (name: string, ...args: unknown[]) => (executed[name] ?? api.commands[name])?.(...(args as [])),
    },
    lm: { registerLanguageModelChatProvider: (_vendor: string, provider: unknown) => ((api.provider = provider), { dispose() {} }) },
  };
  return { api, vscode };
}

/** Load the compiled extension against `vscode`; returns it and a context whose subscriptions can be disposed. */
function activate(vscode: object) {
  const load = (Module as any)._load;
  (Module as any)._load = function (request: string, ...rest: unknown[]) {
    return request === "vscode" ? vscode : load.call(this, request, ...rest);
  };
  try {
    for (const file of ["extension", "provider"]) delete require.cache[require.resolve(`../src/${file}`)];
    const extension = require("../src/extension");
    const context = { subscriptions: [] as Array<{ dispose(): unknown }>, environmentVariableCollection: { replace() {} }, workspaceState: { get() {} } };
    extension.activate(context);
    return context;
  } finally {
    (Module as any)._load = load;
  }
}

/**
 * A stand-in CLI that logs its arguments to calls.log: `profile show` fails (no profile yet), `repo show` prints
 * `labels`, `profile add` fails with `addError` when given, everything else succeeds.
 */
function fakeCli(directory: string, labels: object = {}, addError?: string): string[] {
  const script = path.join(directory, "cli.js");
  fs.writeFileSync(script, `const args = process.argv.slice(2);
require("node:fs").appendFileSync(${JSON.stringify(path.join(directory, "calls.log"))}, JSON.stringify(args) + "\\n");
if (args[0] === "profile" && args[1] === "show") { process.stderr.write("Error: No profile is configured.\\n"); process.exit(1); }
if (args[0] === "repo" && args[1] === "show") console.log(${JSON.stringify(JSON.stringify(labels))});
${addError ? `if (args[0] === "profile" && args[1] === "add") { process.stderr.write(${JSON.stringify(`Error: ${addError}\n`)}); process.exit(1); }` : ""}\n`);
  return [process.execPath, script];
}

const calls = (directory: string): string[][] => fs.readFileSync(path.join(directory, "calls.log"), "utf8")
  .trim().split("\n").map((line) => JSON.parse(line));

const settle = () => new Promise((resolve) => setTimeout(resolve, 50));
const until = async (condition: () => boolean) => {
  for (let attempt = 0; attempt < 100 && !condition(); attempt += 1) await settle();
};

test("closing the sign-in terminal lists the models again", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "vkm-ext-"));
  const { api, vscode } = fakeVscode(undefined, fakeCli(directory), {});
  const context = activate(vscode);
  let changes = 0;
  api.provider.onDidChangeLanguageModelChatInformation(() => (changes += 1));
  api.commands["vkm.signIn"]();
  assert.deepEqual(api.terminals.map((terminal) => terminal.name), ["vkm login"]);
  api.fire("closeTerminal", { name: "some other terminal" });
  assert.equal(changes, 0);
  api.fire("closeTerminal", api.terminals[0]);
  assert.equal(changes, 1);
  context.subscriptions.forEach((item) => item.dispose());
  fs.rmSync(directory, { recursive: true, force: true });
});

test("a 401 that survived the retry offers 'Sign in'", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "vkm-ext-"));
  const { api, vscode } = fakeVscode(undefined, fakeCli(directory), {});
  const context = activate(vscode);
  api.provider.services.onError(new GatewayError("The gateway answered HTTP 401: invalid key", 401));
  api.provider.services.onError(new GatewayError("The gateway answered HTTP 500", 500));
  assert.deepEqual(api.warnings.map((warning) => warning.actions), [["Sign in"], []]);
  context.subscriptions.forEach((item) => item.dispose());
  fs.rmSync(directory, { recursive: true, force: true });
});

test("in a remote window, a new profile and a local sign-in list the models again",
  { skip: process.platform === "win32" && "the relay needs a Unix socket" }, async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "vkm-ext-"));
    const previous = process.env.VKM_AGENT;
    process.env.VKM_AGENT = path.join(directory, "relay", "agent.sock");
    const local = {
      "vkm.local.request": () => ({ ok: true, result: {
        name: "work", gateway: { type: "litellm", url: "https://gateway.example" }, rules: "/home/me/rules.json",
        labels: [{ key: "customer", required: true }], outputs: { tags: { format: "${customer}:${project}" } },
      } }),
      "vkm.local.signIn": () => undefined,
    };
    const { api, vscode } = fakeVscode("dev-container", fakeCli(directory), local);
    let changes = 0;
    const context = activate(vscode);
    try {
      api.provider.onDidChangeLanguageModelChatInformation(() => (changes += 1));
      await until(() => changes > 0);
      assert.equal(changes, 1, "creating the profile lists the models again");
      const added = calls(directory).find((args) => args[0] === "profile" && args[1] === "add")!;
      assert.equal(added.at(-1), '--definitions={"labels":[{"key":"customer","required":true}]}');
      assert.equal(added.includes("--rules"), false);
      assert.match(api.warnings.map((warning) => warning.message).join("\n"),
        /\/home\/me\/rules\.json is a local file, so the organisation's label definitions are missing in this container\. Its output overrides tags use those labels and were left out/);
      await api.commands["vkm.signIn"]();
      assert.equal(changes, 2, "a local sign-in lists the models again");
    } finally {
      context.subscriptions.forEach((item) => item.dispose());
      if (previous === undefined) delete process.env.VKM_AGENT;
      else process.env.VKM_AGENT = previous;
      fs.rmSync(directory, { recursive: true, force: true });
    }
  });

const CLI_LABEL = { name: null, source: "file", detail: "/w/.vkm", required: false, description: null };

test("the status bar shows the label values, and the command to show or assign them is registered", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "vkm-ext-"));
  const labels = {
    labels: [{ ...CLI_LABEL, key: "customer", value: "acme", name: "Acme" }, { ...CLI_LABEL, key: "project", value: "web" }],
    missing: [], ignored: [], repository: null, complete: true, headers: {}, message: null, notices: [],
  };
  const { api, vscode } = fakeVscode(undefined, fakeCli(directory, labels), {}, ["/w"]);
  const context = activate(vscode);
  try {
    await until(() => api.status.text !== undefined);
    assert.equal(api.status.text, "$(tag) Acme · web");
    assert.equal(api.status.backgroundColor, undefined);
    assert.equal((api.status as any).command, "vkm.showLabels");
    assert.ok(api.commands["vkm.showLabels"] && !api.commands["vkm.showCustomer"]);
  } finally {
    context.subscriptions.forEach((item) => item.dispose());
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test("missing required labels block the request, and 'Assign labels' runs repo bind for the folder", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "vkm-ext-"));
  const message = "Required labels have no value: customer for folder /w. Set them with 'vkm repo bind'. Nothing was sent.";
  const labels = {
    labels: [{ ...CLI_LABEL, key: "customer", value: null, source: null, detail: null, required: true }],
    missing: ["customer"], ignored: [], repository: null, complete: false, headers: {}, message, notices: [],
  };
  const { api, vscode } = fakeVscode(undefined, fakeCli(directory, labels), {}, ["/w"]);
  api.action = "Assign labels";
  const context = activate(vscode);
  try {
    const token = { isCancellationRequested: false, onCancellationRequested() {} };
    await assert.rejects(api.provider.provideLanguageModelChatResponse({ id: "m" }, [], {}, { report() {} }, token),
      /^Error: Required labels have no value: customer/);
    assert.equal(calls(directory).some((args) => args[0] === "token"), false, "nothing was sent");
    assert.deepEqual(api.warnings.map((warning) => warning.actions), [["Assign labels"]]);
    await until(() => api.terminals.length > 0);
    assert.deepEqual(api.terminals.map((terminal) => [terminal.name, terminal.shellArgs.slice(1)]),
      [["vkm repo bind", ["repo", "bind", "--directory", "/w"]]]);
    assert.equal(api.status.text, "$(warning) missing customer");
    const before = calls(directory).filter((args) => args[0] === "repo").length;
    api.fire("closeTerminal", api.terminals[0]);
    await until(() => calls(directory).filter((args) => args[0] === "repo").length > before);
    assert.ok(calls(directory).filter((args) => args[0] === "repo").length > before, "the labels are asked for again");
  } finally {
    context.subscriptions.forEach((item) => item.dispose());
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test("in a remote window, a failed profile creation still warns about the skipped rules and shows why it failed",
  { skip: process.platform === "win32" && "the relay needs a Unix socket" }, async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "vkm-ext-"));
    const previous = process.env.VKM_AGENT;
    process.env.VKM_AGENT = path.join(directory, "relay", "agent.sock");
    const local = {
      "vkm.local.request": () => ({ ok: true, result: {
        name: "work", gateway: { type: "litellm", url: "https://gateway.example" }, rules: "/home/me/rules.json",
      } }),
    };
    const { api, vscode } = fakeVscode("dev-container", fakeCli(directory, {}, "Profile 'work' is invalid."), local);
    let changes = 0;
    const context = activate(vscode);
    try {
      api.provider.onDidChangeLanguageModelChatInformation(() => (changes += 1));
      await until(() => api.warnings.length > 1);
      const messages = api.warnings.map((warning) => warning.message);
      assert.equal(messages.length, 2);
      assert.match(messages[0], /rules\.json is a local file/);
      assert.equal(messages[1], "vkm: Could not create a profile from the local one ('work'): Profile 'work' is invalid.");
      assert.equal(changes, 0, "no profile, so the models are not listed again");
    } finally {
      context.subscriptions.forEach((item) => item.dispose());
      if (previous === undefined) delete process.env.VKM_AGENT;
      else process.env.VKM_AGENT = previous;
      fs.rmSync(directory, { recursive: true, force: true });
    }
  });

test("label values, names, missing keys and messages cannot inject icons into the status bar or the quick pick", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "vkm-ext-"));
  const labels = {
    labels: [{ ...CLI_LABEL, key: "customer", value: "acme", name: "$(check) Acme" }],
    missing: ["$(key)"], ignored: [], repository: null, complete: false, headers: {}, message: "Set $(key) first.", notices: [],
  };
  const { api, vscode } = fakeVscode(undefined, fakeCli(directory, labels), {}, ["/w/$(repo)"]);
  const context = activate(vscode);
  try {
    await until(() => api.status.text !== undefined);
    assert.equal(api.status.text, "$(warning) \\$(check) Acme · missing \\$(key)");
    await api.commands["vkm.showLabels"]();
    assert.deepEqual(api.picks[0].map(({ label, description, detail }) => [label, description, detail]),
      [["\\$(repo)", "\\$(check) Acme · missing \\$(key)", "Assign labels: Set \\$(key) first."]]);
  } finally {
    context.subscriptions.forEach((item) => item.dispose());
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
