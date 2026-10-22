// Registers the gateway models for Copilot Chat and shows which labels this window's requests carry.
import * as vscode from "vscode";
import { AGENT_VARIABLE, agentPath, Busy, serve, stillServing, type AgentRequest } from "./agent";
import { choose, escapeIcons, LabelCache, summary, tooltip, type Choice } from "./labels";
import * as cli from "./cli";
import { TokenCache } from "./credentials";
import { GatewayError } from "./gateway";
import { Provider, VENDOR, type Services } from "./provider";

const CHOSEN_FOLDER = "vkm.chosenFolder";
const LOGIN_TERMINAL = "vkm login";
const BIND_TERMINAL = "vkm repo bind";
/** Commands of the companion extension "Virtual Key Mapper: local sign-in", which runs on the local machine. */
const LOCAL_REQUEST = "vkm.local.request";
const LOCAL_SIGN_IN = "vkm.local.signIn";
const LOCAL_MISSING = "Install the extension 'Virtual Key Mapper: local sign-in' in your local VS Code; "
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
        "vkm: install the CLI in this container, e.g. 'pip install <path or URL of virtual-key-mapper>'.",
      );
      return;
    }
  }
  let local: cli.LocalProfile | undefined;
  try {
    local = await askLocal({ operation: "profile", profile: profileName }) as cli.LocalProfile;
    const { args, skippedRules, droppedOutputs } = cli.remoteProfileArgs(local);
    if (skippedRules) {
      // Without the central document, its required labels and output formats do not apply here.
      const dropped = droppedOutputs.length
        ? ` Its output overrides ${droppedOutputs.join(", ")} use those labels and were left out (the gateway's defaults apply).`
        : "";
      const warning = `Profile '${local.name}' here lacks its central label rules: ${skippedRules} is a local file, so the `
        + "organisation's label definitions are missing in this container." + dropped + " Set them with "
        + `'vkm profile update ${local.name} --rules URL_OR_PATH'.`;
      output.appendLine(warning);
      void vscode.window.showWarningMessage(`vkm: ${warning}`);
    }
    await run(args);
    output.appendLine(`Created profile '${local.name}' here; it uses the sign-in of your local machine.`);
    onCreated(); // The first model listing ran without a profile and found nothing.
  } catch (error) {
    const message = `Could not create a profile from the local one${local ? ` ('${local.name}')` : ""}: ${(error as Error).message}`;
    output.appendLine(message);
    void vscode.window.showWarningMessage(`vkm: ${message}`);
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("Virtual Key Mapper");
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  status.command = "vkm.showLabels";
  status.name = "Virtual Key Mapper labels";
  let lastError = "";

  const settings = () => vscode.workspace.getConfiguration("vkm");
  const command = () => settings().get<string[]>("command", ["vkm"]);
  const profileName = () => settings().get<string>("profile", "").trim();

  const terminal = (name: string, args: string[]) => {
    const [executable, ...prefix] = command();
    vscode.window.createTerminal({ name, shellPath: executable, shellArgs: [...prefix, ...args] }).show();
  };

  const warn = (warning: boolean) =>
    (status.backgroundColor = warning ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined);
  const showChoice = (choice: Choice) => {
    if (choice.kind === "folder") {
      const shown = summary(choice.labels);
      status.text = `$(${shown.warning ? "warning" : "tag"}) ${shown.text}`;
      status.tooltip = tooltip(choice.labels, choice.folder);
      warn(shown.warning);
    } else {
      status.text = choice.kind === "none" ? "$(tag) no folder" : "$(warning) choose folder";
      status.tooltip = choice.kind === "none" ? "vkm: open a folder to use its labels"
        : "vkm: the workspace folders have different labels; click to choose";
      warn(choice.kind === "choose");
    }
    status.show();
  };
  const bind = (directory: string) => terminal(BIND_TERMINAL, cli.bindArgs(profileName(), directory));

  const showError = (error: unknown) => {
    const message = error instanceof Error ? error.message : String(error);
    output.appendLine(`[${new Date().toISOString()}] ${message}`);
    if (message === lastError) return; // One notification per distinct problem.
    lastError = message;
    // A 401 here survived the retry with a fresh token: the gateway no longer accepts this sign-in.
    const needsLogin = (error instanceof cli.CliError && error.needsLogin) || (error instanceof GatewayError && error.status === 401);
    const missing = error instanceof cli.CliError && error.missingLabels ? error : undefined;
    const actions = needsLogin ? ["Sign in"] : missing ? ["Assign labels"] : [];
    void vscode.window.showWarningMessage(`vkm: ${message}`, ...actions).then((action) => {
      if (action === "Sign in") vscode.commands.executeCommand("vkm.signIn");
      if (action === "Assign labels") {
        if (missing?.directory) bind(missing.directory);
        else vscode.commands.executeCommand("vkm.showLabels");
      }
    });
  };

  const services = (): Services => {
    const run = cli.runner(command());
    const name = profileName();
    return {
      run, profileName: name,
      tokens: new TokenCache(() => cli.token(run, name)),
      labels: new LabelCache((folder) => cli.labels(run, name, folder)),
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
      showChoice(await choose(folders, active, current.chosenFolder(), current.labels));
    } catch (error) {
      status.text = "$(warning) labels";
      status.tooltip = error instanceof Error ? error.message : String(error);
      warn(true);
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
    vscode.workspace.onDidChangeConfiguration((event) => event.affectsConfiguration("vkm") && reset()),
    vscode.workspace.onDidChangeWorkspaceFolders(() => void refreshStatus()),
    vscode.window.onDidChangeActiveTextEditor(() => void refreshStatus()),
    vscode.commands.registerCommand("vkm.refresh", reset),
    vscode.window.onDidCloseTerminal((closed) => {
      if (closed.name === LOGIN_TERMINAL) reset(); // List the models again: a listing without a sign-in found none.
      if (closed.name === BIND_TERMINAL) {
        current.labels.clear(); // The new binding applies at once, not after the cache expires.
        lastError = "";
        void refreshStatus();
      }
    }),
    vscode.commands.registerCommand("vkm.signIn", async () => {
      if (!vscode.env.remoteName) return terminal(LOGIN_TERMINAL, cli.loginArgs(profileName()));
      try {
        await vscode.commands.executeCommand(LOCAL_SIGN_IN); // Remote windows sign in on the local machine.
        reset();
      } catch {
        void vscode.window.showWarningMessage(LOCAL_MISSING);
      }
    }),
    vscode.commands.registerCommand("vkm.showLabels", async () => {
      const folders = vscode.workspace.workspaceFolders ?? [];
      current.labels.clear();
      const items = await Promise.all(folders.map(async (folder) => {
        try {
          const labels = await current.labels.get(folder.uri.fsPath);
          const sources = labels.labels.filter((label) => label.source).map((label) => `${label.key}: ${label.source}`);
          return {
            label: escapeIcons(folder.name), description: summary(labels).text, folder, assign: !labels.complete,
            detail: escapeIcons(labels.complete ? sources.join(", ") : `Assign labels: ${labels.message ?? ""}`),
          };
        } catch (error) {
          const detail = escapeIcons(error instanceof Error ? error.message : String(error));
          return { label: escapeIcons(folder.name), description: "labels", detail, folder, assign: true };
        }
      }));
      const picked = await vscode.window.showQuickPick(items, {
        title: "Labels for Copilot Chat requests in this window",
        placeHolder: "Choose the folder whose labels apply when no editor is active",
      });
      if (!picked) return;
      if (picked.assign) {
        bind(picked.folder.uri.fsPath);
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
