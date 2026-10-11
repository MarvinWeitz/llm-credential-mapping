// The real pair on Linux: the extension's relay (Node, Unix socket) answering the CLI's forwarded sign-in (Python).
// Needs `python3` with the llm-credential-mapping package installed (CI installs it); skipped elsewhere.
import assert from "node:assert/strict";
import { execFile, execFileSync, spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { serve } from "../src/agent";

const python = process.env.LLM_CREDENTIALS_TEST_PYTHON ?? "python3";
const available = process.platform !== "win32"
  && spawnSync(python, ["-c", "import llm_credential_mapping.auth.forwarded"]).status === 0;

test("the CLI's forwarded sign-in gets its token from the relay", { skip: !available && "needs Linux and the CLI" }, async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "llmc-interop-"));
  const socket = path.join(directory, "relay", "agent.sock");
  const home = path.join(directory, "home");
  const seen: unknown[] = [];
  const server = await serve(socket, async (request) => {
    seen.push(request);
    return { access_token: "relayed-token", expires_at: null, profile: "work", user: "me" };
  });
  try {
    const env = { ...process.env, LLM_CREDENTIALS_AGENT: socket };
    const cli = (...args: string[]) => new Promise<string>((resolve, reject) => {
      // Asynchronous: the relay answers on this process's event loop.
      execFile(python, ["-m", "llm_credential_mapping", "--home", home, ...args], { env },
        (error, stdout, stderr) => (error ? reject(new Error(stderr)) : resolve(stdout)));
    });
    await cli("profile", "add", "container", "--gateway", "https://gateway.example", "--auth", "forwarded", "--host-profile", "work");
    assert.equal(await cli("token"), "relayed-token\n");
    assert.deepEqual(seen, [{ operation: "token", profile: "work" }]);
  } finally {
    server.close();
    execFileSync("rm", ["-rf", directory]);
  }
});
