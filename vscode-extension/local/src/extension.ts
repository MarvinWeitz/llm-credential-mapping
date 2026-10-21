// Runs on the local machine (extensionKind "ui"), also for dev container and SSH windows. Remote windows ask it
// for credentials through VS Code commands; it answers with the local CLI and its OS vault.
import { execFile } from "node:child_process";
import * as vscode from "vscode";
import { answer, RunError, type Run } from "./handler";

/**
 * The local CLI. `vkm.localCommand` is application-scoped, so it is read from the local user settings in
 * every window; the main extension's machine-scoped `vkm.command` is hidden in remote windows.
 */
function localCommand(): string[] {
  const settings = vscode.workspace.getConfiguration("vkm");
  const local = settings.get<string[]>("localCommand", []);
  if (Array.isArray(local) && local.length) return local;
  const command = settings.inspect<string[]>("command")?.globalValue;
  return Array.isArray(command) && command.length ? command : ["vkm"];
}

function runner(timeout = 60000): Run {
  const command = localCommand();
  return (args) => new Promise((resolve, reject) => {
    execFile(command[0], [...command.slice(1), ...args], { windowsHide: true, maxBuffer: 4 << 20, timeout }, (error, stdout, stderr) => {
      if (!error) return resolve(stdout);
      const notFound = (error as NodeJS.ErrnoException).code === "ENOENT";
      reject(new RunError(notFound
        ? `Cannot run ${command[0]} on this computer; set vkm.localCommand in your user settings.`
        : String(stderr || error.message).replace(/^Error: /, "").trim()));
    });
  });
}

/**
 * The local profiles remote windows may use; empty: the local default only. Application-scoped like localCommand, so
 * neither a workspace nor the remote side can change it (`vkm.profile` would be the remote window's value).
 */
function localProfiles(): string[] {
  const names = vscode.workspace.getConfiguration("vkm").get<string[]>("localProfiles", []);
  return Array.isArray(names) ? names.filter((name) => typeof name === "string" && name.trim()).map((name) => name.trim()) : [];
}

export function activate(context: vscode.ExtensionContext): void {
  let lastPrompt = "";
  let lastProfile = ""; // The profile whose request needed a sign-in.
  context.subscriptions.push(
    vscode.commands.registerCommand("vkm.local.request", async (request: unknown) => {
      const result = await answer(request, runner(), localProfiles());
      if (!result.ok && result.login && result.error !== lastPrompt) {
        lastPrompt = result.error; // One prompt per distinct problem.
        lastProfile = result.profile;
        void vscode.window.showWarningMessage(`vkm: ${result.error}`, "Sign in").then((action) => {
          if (action === "Sign in") vscode.commands.executeCommand("vkm.local.signIn");
        });
      }
      return result;
    }),
    vscode.commands.registerCommand("vkm.local.signIn", () =>
      vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "vkm: complete the sign-in in your browser", cancellable: false },
        async () => {
          const name = lastProfile || localProfiles()[0] || "";
          try {
            const output = await runner(15 * 60000)(["login", ...(name ? ["--profile", name] : [])]);
            lastPrompt = "";
            void vscode.window.showInformationMessage(output.trim() || "Signed in.");
          } catch (error) {
            void vscode.window.showErrorMessage(`vkm: ${(error as Error).message}`);
          }
        },
      )),
  );
}

export function deactivate(): void {}
