import * as vscode from "vscode";
import { randomUUID } from "node:crypto";
import { lstat, mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import * as path from "node:path";
import { Cli, CliRuntime, IntegrationError, parseProfiles, Profile, record } from "./cli";
import { configureClaudeDocument, helperCommand, sameProfile, validateClaudeEnvironment } from "./claude";
import { Gateway, overrides } from "./protocol";
import { Provider } from "./provider";

function runtime(): CliRuntime {
  const config = vscode.workspace.getConfiguration("llmCredentials");
  const executable = config.get<unknown>("cliExecutable", "");
  const args = config.get<unknown>("cliArguments", []);
  const home = config.get<unknown>("homeDirectory", "");
  const dbusAddress = config.get<unknown>("dbusSessionBusAddress", "");
  if (typeof executable !== "string" || !executable || !Array.isArray(args)
    || !args.every((item: unknown) => typeof item === "string")
    || typeof home !== "string" || typeof dbusAddress !== "string") {
    throw new IntegrationError("Configure a CLI executable and string-array arguments in User/Remote Settings.");
  }
  return { executable, args, home, dbusAddress };
}

function trusted(): void {
  if (!vscode.workspace.isTrusted || !vscode.workspace.workspaceFolders?.length) {
    throw new IntegrationError("Open a trusted workspace before configuring project credentials.");
  }
}

function missing(error: unknown): boolean { return record(error) && error.code === "ENOENT"; }

async function localClaudeText(filename: string): Promise<string> {
  try { return await readFile(filename, "utf8"); }
  catch (error) { if (!missing(error)) throw error; return ""; }
}

async function rejectSymlink(target: string): Promise<void> {
  try {
    if ((await lstat(target)).isSymbolicLink()) throw new IntegrationError("Claude local settings must not be symlinked to another configuration.");
  } catch (error) { if (!missing(error)) throw error; }
}

export function activate(context: vscode.ExtensionContext): void {
  const binding = (): Profile | undefined => {
    const value = context.workspaceState.get<unknown>("project");
    return value === undefined ? undefined : parseProfiles({ version: 1, projects: [value] })[0];
  };
  const cli = () => new Cli(runtime(), (message) => { void vscode.window.showWarningMessage(message); });
  const provider = new Provider({
    binding, cli, overrides: () => overrides(vscode.workspace.getConfiguration("llmCredentials").get<unknown>("modelOverrides", [])),
  });
  const bar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 20);
  bar.command = "llmCredentials.selectProject";
  const update = () => {
    const profile = binding();
    bar.text = profile ? `$(key) LLM: ${profile.name}` : "$(key) LLM: Select Project";
    bar.tooltip = profile ? `${profile.gateway}\nTeam ${profile.team_id}\nExplicit workspace binding; click to switch.` : "Select a LiteLLM project for this workspace.";
    bar.show();
  };
  const command = (name: string, action: () => Promise<void>) => vscode.commands.registerCommand("llmCredentials." + name, async () => {
    try { trusted(); await action(); }
    catch (error) {
      await vscode.window.showErrorMessage(error instanceof IntegrationError ? error.message : "LLM integration failed; check configuration and filesystem access.");
    }
  });
  const selected = async () => {
    const expected = binding();
    if (!expected) throw new IntegrationError("Use LLM: Select Project first.");
    const client = cli();
    const profile = await client.profile(expected.name);
    if (!sameProfile(profile, expected)) throw new IntegrationError("Project mapping changed. Select the workspace project again.");
    return { profile, client };
  };
  context.subscriptions.push(provider, bar,
    vscode.lm.registerLanguageModelChatProvider("cbs-litellm", provider),
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration("llmCredentials")) provider.refresh();
    }),
    command("selectProject", async () => {
      const profiles = await cli().profiles();
      if (!profiles.length) throw new IntegrationError("No CLI projects configured. Run 'project add NAME --gateway URL --team-id ID'.");
      const item = await vscode.window.showQuickPick(profiles.map((profile) => ({
        label: profile.name, description: profile.gateway, detail: `LiteLLM team ${profile.team_id}`, profile,
      })), { title: "Select LiteLLM project for this workspace" });
      if (!item) return;
      const previous = binding();
      if (previous && !sameProfile(previous, item.profile)) {
        const answer = await vscode.window.showWarningMessage(
          "New Copilot requests require this project's model. In-flight requests stay on their original project. Close existing Claude conversations and reconfigure Claude before switching.",
          { modal: true }, "Switch Project");
        if (answer !== "Switch Project") return;
      }
      await context.workspaceState.update("project", item.profile);
      provider.refresh();
      update();
    }),
    command("login", async () => {
      const { profile, client } = await selected();
      const terminal = vscode.window.createTerminal({
        name: `LLM Login: ${profile.name}`, shellPath: client.runtime.executable,
        shellArgs: client.arguments(["login", "--project", profile.name, "--no-browser", "--timeout", "300"]),
        env: client.environment(),
      });
      terminal.show();
      await vscode.window.showInformationMessage(
        "Open the authorization URL shown in the terminal. In a remote/container workspace, forward the callback's exact localhost port. Then run LLM: Show Status.");
    }),
    command("status", async () => {
      const { profile, client } = await selected();
      const output = await client.run(["status", "--project", profile.name]);
      let status: unknown;
      try { status = JSON.parse(output); } catch { throw new IntegrationError("CLI returned invalid status JSON."); }
      if (!record(status) || typeof status.state !== "string") throw new IntegrationError("CLI returned invalid status.");
      await vscode.window.showInformationMessage(`LLM ${profile.name}: ${status.state}${typeof status.user_id === "string" ? ` / employee ${status.user_id}` : ""}`);
      provider.refresh();
    }),
    command("configureCli", async () => {
      await vscode.commands.executeCommand("workbench.action.openSettings", "@ext:cbs-consulting.llm-credential-mapping");
    }),
    command("configureModels", async () => {
      const { profile, client } = await selected();
      const catalog = await new Gateway().json(profile, await client.token(profile), "/v1/models", AbortSignal.timeout(30_000));
      if (!record(catalog) || !Array.isArray(catalog.data)) throw new IntegrationError("Gateway returned invalid model catalog.");
      const ids = catalog.data.map((item: unknown) => {
        if (!record(item) || typeof item.id !== "string") throw new IntegrationError("Invalid gateway model alias.");
        return item.id;
      });
      const id = await vscode.window.showQuickPick(ids, { title: "Override metadata for an allowed LiteLLM alias" });
      if (!id) return;
      const limits: number[] = [];
      for (const label of ["Maximum input tokens", "Maximum output tokens"]) {
        const value = await vscode.window.showInputBox({
          title: label, prompt: "Use the actual model/gateway limit; do not guess.",
          validateInput: (value) => /^\d+$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value) > 0 ? undefined : "Enter a positive integer.",
        });
        if (value === undefined) return;
        limits.push(Number(value));
      }
      const capability = await vscode.window.showQuickPick(["Text and tools", "Text only"], { title: "Verified model capabilities" });
      if (!capability) return;
      const vision = await vscode.window.showQuickPick(["No images", "Images supported"], { title: "Verified image capability" });
      if (!vision) return;
      const config = vscode.workspace.getConfiguration("llmCredentials");
      const existing = overrides(config.get<unknown>("modelOverrides", []));
      await config.update("modelOverrides", [
        ...existing.filter((item) => item.project !== profile.name || item.id !== id),
        { project: profile.name, id, maxInputTokens: limits[0], maxOutputTokens: limits[1], toolCalling: capability === "Text and tools", imageInput: vision === "Images supported" },
      ], vscode.ConfigurationTarget.Workspace);
      provider.refresh();
    }),
    command("configureClaude", async () => {
      const folders = vscode.workspace.workspaceFolders;
      if (!folders || folders.length !== 1 || !folders[0]) throw new IntegrationError("Claude setup currently requires a single-folder workspace.");
      const { profile, client } = await selected();
      const alias = await vscode.window.showInputBox({ title: "Claude-compatible LiteLLM model alias", prompt: "The gateway must support Claude's Messages API for this alias." });
      if (alias === undefined) return;
      validateClaudeEnvironment(vscode.workspace.getConfiguration("claudeCode").get<unknown>("environmentVariables", []), process.env, profile, alias);
      const root = folders[0].uri.fsPath;
      const directory = path.join(root, ".claude");
      const filename = path.join(directory, "settings.local.json");
      await Promise.all([rejectSymlink(directory), rejectSymlink(filename)]);
      const text = await localClaudeText(filename);
      const document = vscode.workspace.textDocuments.find((item) => item.uri.fsPath === filename);
      if (document?.isDirty) throw new IntegrationError("Save or discard unsaved Claude settings edits before configuring.");
      const updated = configureClaudeDocument(text, profile, helperCommand(client, profile.name), alias);
      const answer = await vscode.window.showWarningMessage(
        `Configure Claude for '${profile.name}' in this repository? Existing gateway/auth/model settings are replaced; unrelated settings/comments are preserved. Close Claude conversations first. No tokens are written.`,
        { modal: true }, "Configure Claude");
      if (answer !== "Configure Claude") return;
      await Promise.all([rejectSymlink(directory), rejectSymlink(filename)]);
      if (await localClaudeText(filename) !== text || document?.isDirty) {
        throw new IntegrationError("Claude settings changed while configuring. Save your changes and run the command again.");
      }
      await mkdir(directory, { recursive: true });
      const temporary = path.join(directory, `.llm-mapping-${randomUUID()}.tmp`);
      try {
        await writeFile(temporary, updated, { encoding: "utf8", flag: "wx", mode: 0o600 });
        await rename(temporary, filename);
      } finally {
        try { await unlink(temporary); } catch (error) { if (!missing(error)) throw error; }
      }
      await vscode.workspace.getConfiguration("claudeCode", folders[0].uri).update("disableLoginPrompt", true, vscode.ConfigurationTarget.Workspace);
      await vscode.window.showInformationMessage(
        "Claude configured without stored tokens. Restart Claude/new conversation; verify its gateway/model. Keep .claude/settings.local.json out of Git. Conflicting machine/user Claude environment settings must be removed.");
    }),
  );
  update();
}
