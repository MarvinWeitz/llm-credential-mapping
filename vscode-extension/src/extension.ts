// Registers the gateway models for Copilot Chat and shows which customer this window's requests use.
import * as vscode from "vscode";
import { AGENT_VARIABLE, agentPath, Busy, serve, stillServing, type AgentRequest } from "./agent";
import { AttributionCache, choose, type Choice } from "./attribution";
import * as cli from "./cli";
import { TokenCache } from "./credentials";
import { GatewayError } from "./gateway";
import { Provider, VENDOR, type Services } from "./provider";

const CHOSEN_FOLDER = "llmCredentials.chosenFolder";
const LOGIN_TERMINAL = "llm-credentials login";
/** Commands of the companion extension "LLM Credentials: local sign-in", which runs on the local machine. */
const LOCAL_REQUEST = "llmCredentials.local.request";
const LOCAL_SIGN_IN = "llmCredentials.local.signIn";
const LOCAL_MISSING = "Install the extension 'LLM Credentials: local sign-in' in your local VS Code; "
  + "it relays your local sign-in into this window.";

/** Ask the companion extension on the local machine; its answer is {ok, result} or {ok: false, error}. */
async function askLocal(request: AgentRequest): Promise<unknown> {
  let answer: any;
  try {
    answer = await vscode.commands.executeCommand(LOCAL_REQUEST, request);
  } catch {
    throw new Error(LOCAL_MISSING);
  }
  if (answer?.ok === true) return answer.result;
  throw new Error(typeof answer?.error === "string" ? answer.error : LOCAL_MISSING);
}

/**
 * In a remote window (dev container, SSH, WSL): serve the CLI's "forwarded" sign-in and make sure the remote
 * CLI has a profile using it, created from the local profile. The refresh grant never leaves the local vault.
 */
function startRelay(
  context: vscode.ExtensionContext, output: vscode.OutputChannel, command: () => string[], profileName: () => string,
  onCreated: () => void,
) {
  const socketPath = agentPath();
  context.environmentVariableCollection.replace(AGENT_VARIABLE, socketPath);
  let stopped = false;
  const start = async () => {
    try {
      const server = await serve(socketPath, askLocal);
      // Another window starting at the same moment may have replaced the socket: serve again, or wait as Busy.
      const watch = setInterval(() => {
        if (stillServing(server)) return;
        clearInterval(watch);
        server.close();
        if (!stopped) void start();
      }, 30000);
      context.subscriptions.push({ dispose: () => (clearInterval(watch), server.close()) });
      output.appendLine(`Relaying the local sign-in on ${socketPath}.`);
    } catch (error) {
      output.appendLine(`Local sign-in relay: ${(error as Error).message}`);
      if (error instanceof Busy && !stopped) setTimeout(start, 30000); // Take over when the other window closes.
    }
  };
  context.subscriptions.push({ dispose: () => (stopped = true) });
  void start().then(() => ensureRemoteProfile(command(), profileName(), output, onCreated));
}

async function ensureRemoteProfile(
  command: string[], profileName: string, output: vscode.OutputChannel, onCreated: () => void,
): Promise<void> {
  const run = cli.runner(command);
  try {
    await cli.profile(run, profileName);
    return; // A profile already exists; the user decides how it signs in.
  } catch (error) {
    if (error instanceof cli.CliError && /Cannot run/.test(error.message)) {
      void vscode.window.showWarningMessage(
        "llm-credentials: install the CLI in this container, e.g. 'pip install <path or URL of llm-credential-mapping>'.",
      );
      return;
    }
  }
  try {
    const local = await askLocal({ operation: "profile", profile: profileName }) as cli.LocalProfile;
    const { args, skippedRules } = cli.remoteProfileArgs(local);
    await run(args);
    output.appendLine(`Created profile '${local.name}' here; it uses the sign-in of your local machine.`);
    if (skippedRules) {
      output.appendLine(`Its central customer rules (${skippedRules}) are a local file; set them here with `
        + `'llm-credentials profile update ${local.name} --rules URL_OR_PATH'.`);
    }
    onCreated(); // The first model listing ran without a profile and found nothing.
  } catch (error) {
    output.appendLine(`Could not create a profile from the local one: ${(error as Error).message}`);
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("LLM Credentials");
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  status.command = "llmCredentials.showCustomer";
  status.name = "LLM Credentials customer";
  let lastError = "";

  const settings = () => vscode.workspace.getConfiguration("llmCredentials");
  const command = () => settings().get<string[]>("command", ["llm-credentials"]);
  const profileName = () => settings().get<string>("profile", "").trim();

  const terminal = (name: string, args: string[]) => {
    const [executable, ...prefix] = command();
    vscode.window.createTerminal({ name, shellPath: executable, shellArgs: [...prefix, ...args] }).show();
  };

  const showChoice = (choice: Choice) => {
    if (choice.kind === "folder") {
      status.text = `$(tag) ${choice.attribution.customer}`;
      status.tooltip = `Copilot Chat requests with llm-credentials models use customer ${choice.attribution.customer}\n`
        + `${choice.attribution.source}: ${choice.attribution.detail}\nFolder: ${choice.folder}`;
      status.backgroundColor = undefined;
    } else {
      status.text = choice.kind === "none" ? "$(tag) no folder" : "$(warning) choose customer";
      status.tooltip = "llm-credentials: no customer for this window; click to choose";
      status.backgroundColor = new vscode.ThemeColor("statusBarItem.warningBackground");
    }
    status.show();
  };

  const showError = (error: unknown) => {
    const message = error instanceof Error ? error.message : String(error);
    output.appendLine(`[${new Date().toISOString()}] ${message}`);
    if (message === lastError) return; // One notification per distinct problem.
    lastError = message;
    // A 401 here survived the retry with a fresh token: the gateway no longer accepts this sign-in.
    const needsLogin = (error instanceof cli.CliError && error.needsLogin) || (error instanceof GatewayError && error.status === 401);
    const actions = needsLogin ? ["Sign in"]
      : error instanceof cli.CliError && error.unassigned ? ["Assign customer"] : [];
    void vscode.window.showWarningMessage(`llm-credentials: ${message}`, ...actions).then((action) => {
      if (action === "Sign in") vscode.commands.executeCommand("llmCredentials.signIn");
      if (action === "Assign customer") vscode.commands.executeCommand("llmCredentials.showCustomer");
    });
  };

  const services = (): Services => {
    const run = cli.runner(command());
    const name = profileName();
    return {
      run, profileName: name,
      tokens: new TokenCache(() => cli.token(run, name)),
      customers: new AttributionCache((folder) => cli.attribution(run, name, folder)),
      chosenFolder: () => context.workspaceState.get<string>(CHOSEN_FOLDER),
      onChoice: showChoice,
      onError: showError,
    };
  };

  let current = services();
  const provider = new Provider(current);
  const refreshStatus = async () => {
    const folders = (vscode.workspace.workspaceFolders ?? []).map((folder) => folder.uri.fsPath);
    const document = vscode.window.activeTextEditor?.document.uri;
    const active = document ? vscode.workspace.getWorkspaceFolder(document)?.uri.fsPath : undefined;
    try {
      showChoice(await choose(folders, active, current.chosenFolder(), current.customers));
    } catch (error) {
      status.text = "$(warning) no customer";
      status.tooltip = error instanceof Error ? error.message : String(error);
      status.backgroundColor = new vscode.ThemeColor("statusBarItem.warningBackground");
      status.show();
    }
  };
  const reset = () => {
    current = services();
    lastError = "";
    provider.reset(current);
    void refreshStatus();
  };

  context.subscriptions.push(
    output, status,
    vscode.lm.registerLanguageModelChatProvider(VENDOR, provider),
    vscode.workspace.onDidChangeConfiguration((event) => event.affectsConfiguration("llmCredentials") && reset()),
    vscode.workspace.onDidChangeWorkspaceFolders(() => void refreshStatus()),
    vscode.window.onDidChangeActiveTextEditor(() => void refreshStatus()),
    vscode.commands.registerCommand("llmCredentials.refresh", reset),
    // After a sign-in, list the models again: a listing without a sign-in found none.
    vscode.window.onDidCloseTerminal((closed) => closed.name === LOGIN_TERMINAL && reset()),
    vscode.commands.registerCommand("llmCredentials.signIn", async () => {
      if (!vscode.env.remoteName) return terminal(LOGIN_TERMINAL, cli.loginArgs(profileName()));
      try {
        await vscode.commands.executeCommand(LOCAL_SIGN_IN); // Remote windows sign in on the local machine.
        reset();
      } catch {
        void vscode.window.showWarningMessage(LOCAL_MISSING);
      }
    }),
    vscode.commands.registerCommand("llmCredentials.showCustomer", async () => {
      const folders = vscode.workspace.workspaceFolders ?? [];
      current.customers.clear();
      const items = await Promise.all(folders.map(async (folder) => {
        try {
          const attribution = await current.customers.get(folder.uri.fsPath);
          return { label: folder.name, description: attribution.customer, detail: `${attribution.source}: ${attribution.detail}`, folder };
        } catch (error) {
          return { label: folder.name, description: "no customer", detail: error instanceof Error ? error.message : String(error), folder };
        }
      }));
      const picked = await vscode.window.showQuickPick(items, {
        title: "Customer for Copilot Chat requests in this window",
        placeHolder: "Choose the folder whose customer applies when no editor is active",
      });
      if (!picked) return;
      if (picked.description === "no customer") {
        terminal("llm-credentials repo bind", cli.bindArgs(profileName(), picked.folder.uri.fsPath));
        return;
      }
      await context.workspaceState.update(CHOSEN_FOLDER, picked.folder.uri.fsPath);
      void refreshStatus();
    }),
  );
  if (vscode.env.remoteName && process.platform !== "win32") startRelay(context, output, command, profileName, reset);
  void refreshStatus();
}

export function deactivate(): void {}
