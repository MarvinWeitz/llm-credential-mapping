// Registers the gateway models for Copilot Chat and shows which customer this window's requests use.
import * as vscode from "vscode";
import { AttributionCache, choose, type Choice } from "./attribution";
import * as cli from "./cli";
import { TokenCache } from "./credentials";
import { Provider, VENDOR, type Services } from "./provider";

const CHOSEN_FOLDER = "llmCredentials.chosenFolder";

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
    const actions = error instanceof cli.CliError && error.needsLogin ? ["Sign in"]
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
    vscode.commands.registerCommand("llmCredentials.signIn", () => terminal("llm-credentials login", cli.loginArgs(profileName()))),
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
  void refreshStatus();
}

export function deactivate(): void {}
