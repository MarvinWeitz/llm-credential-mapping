// Runs on the local machine (extensionKind "ui"), also for dev container and SSH windows. Remote windows ask it
// for credentials through VS Code commands; it answers with the local CLI and its OS vault.
import { execFile } from "node:child_process";
import * as vscode from "vscode";
import { answer, RunError, type Run } from "./handler";

function runner(timeout = 60000): Run {
  const settings = vscode.workspace.getConfiguration("llmCredentials");
  const command = settings.get<string[]>("command", ["llm-credentials"]);
  return (args) => new Promise((resolve, reject) => {
    execFile(command[0], [...command.slice(1), ...args], { windowsHide: true, maxBuffer: 4 << 20, timeout }, (error, stdout, stderr) => {
      if (!error) return resolve(stdout);
      const notFound = (error as NodeJS.ErrnoException).code === "ENOENT";
      reject(new RunError(notFound
        ? `Cannot run ${command[0]} on this computer; set llmCredentials.command in your local user settings.`
        : String(stderr || error.message).replace(/^Error: /, "").trim()));
    });
  });
}

const profileName = () => vscode.workspace.getConfiguration("llmCredentials").get<string>("profile", "").trim();

export function activate(context: vscode.ExtensionContext): void {
  let lastPrompt = "";
  context.subscriptions.push(
    vscode.commands.registerCommand("llmCredentials.local.request", async (request: unknown) => {
      const result = await answer(request, runner(), profileName());
      if (!result.ok && result.login && result.error !== lastPrompt) {
        lastPrompt = result.error; // One prompt per distinct problem.
        void vscode.window.showWarningMessage(`llm-credentials: ${result.error}`, "Sign in").then((action) => {
          if (action === "Sign in") vscode.commands.executeCommand("llmCredentials.local.signIn");
        });
      }
      return result;
    }),
    vscode.commands.registerCommand("llmCredentials.local.signIn", () =>
      vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "llm-credentials: complete the sign-in in your browser", cancellable: false },
        async () => {
          const name = profileName();
          try {
            const output = await runner(15 * 60000)(["login", ...(name ? ["--profile", name] : [])]);
            lastPrompt = "";
            void vscode.window.showInformationMessage(output.trim() || "Signed in.");
          } catch (error) {
            void vscode.window.showErrorMessage(`llm-credentials: ${(error as Error).message}`);
          }
        },
      )),
  );
}

export function deactivate(): void {}
