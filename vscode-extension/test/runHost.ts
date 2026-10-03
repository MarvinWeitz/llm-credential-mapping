import { runTests } from "@vscode/test-electron";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import * as path from "node:path";

async function main(): Promise<void> {
  const folder = await mkdtemp(path.join(tmpdir(), "llm-vscode-smoke-"));
  try {
    await runTests({
      ...(process.env.VSCODE_EXECUTABLE_PATH ? { vscodeExecutablePath: process.env.VSCODE_EXECUTABLE_PATH } : { version: "1.140.0" }),
      extensionDevelopmentPath: path.resolve(__dirname, "..", ".."),
      extensionTestsPath: path.join(__dirname, "host.js"),
      extensionTestsEnv: { LLM_MAPPING_TEST_NODE: process.execPath },
      launchArgs: [
        folder, "--user-data-dir", path.join(folder, "user-data"),
        "--extensions-dir", path.join(folder, "extensions"), "--disable-workspace-trust",
        "--skip-welcome", "--skip-release-notes", "--disable-gpu",
      ],
    });
  } finally { await rm(folder, { recursive: true, force: true }); }
}

main().catch((error: unknown) => { console.error(error); process.exitCode = 1; });
