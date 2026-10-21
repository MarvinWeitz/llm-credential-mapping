// activate() with a stand-in `vscode` module: what the extension does after a sign-in, a new profile or a 401.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import Module from "node:module";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { GatewayError } from "../src/gateway";

type Listener = (value: any) => unknown;

/** A minimal vscode API; `fire` triggers a registered event, `commands` holds the registered command handlers. */
function fakeVscode(remoteName: string | undefined, command: string[], executed: Record<string, () => unknown>) {
  const listeners: Record<string, Listener[]> = {};
  const event = (name: string) => (listener: Listener) => {
    (listeners[name] ??= []).push(listener);
    return { dispose() {} };
  };
  const api = {
    warnings: [] as Array<{ message: string; actions: string[] }>,
    terminals: [] as Array<{ name: string }>,
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
      createStatusBarItem: () => ({ show() {}, dispose() {} }),
      createTerminal: (options: { name: string }) => {
        const terminal = { name: options.name, show() {} };
        api.terminals.push(terminal);
        return terminal;
      },
      showWarningMessage: async (message: string, ...actions: string[]) => (api.warnings.push({ message, actions }), undefined),
      onDidCloseTerminal: event("closeTerminal"),
      onDidChangeActiveTextEditor: event("activeEditor"),
      activeTextEditor: undefined,
    },
    workspace: {
      workspaceFolders: [],
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

/** A stand-in CLI: `profile show` fails (no profile yet), everything else succeeds. */
function fakeCli(directory: string): string[] {
  const script = path.join(directory, "cli.js");
  fs.writeFileSync(script, `if (process.argv[2] === "profile" && process.argv[3] === "show") {
    process.stderr.write("Error: No profile is configured.\\n"); process.exit(1); }\n`);
  return [process.execPath, script];
}

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
      "vkm.local.request": () => ({ ok: true, result: { name: "work", gateway: { type: "litellm", url: "https://gateway.example" } } }),
      "vkm.local.signIn": () => undefined,
    };
    const { api, vscode } = fakeVscode("dev-container", fakeCli(directory), local);
    let changes = 0;
    const context = activate(vscode);
    try {
      api.provider.onDidChangeLanguageModelChatInformation(() => (changes += 1));
      await until(() => changes > 0);
      assert.equal(changes, 1, "creating the profile lists the models again");
      await api.commands["vkm.signIn"]();
      assert.equal(changes, 2, "a local sign-in lists the models again");
    } finally {
      context.subscriptions.forEach((item) => item.dispose());
      if (previous === undefined) delete process.env.VKM_AGENT;
      else process.env.VKM_AGENT = previous;
      fs.rmSync(directory, { recursive: true, force: true });
    }
  });
