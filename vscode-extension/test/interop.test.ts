// The real pair on Linux and macOS: the extension's relay (Node, Unix socket) answering the CLI's forwarded sign-in (Python).
// Needs `python3` with the virtual-key-mapper package installed (CI installs it); skipped elsewhere.
import assert from "node:assert/strict";
import { execFile, execFileSync, spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { serve } from "../src/agent";
import { remoteProfileArgs } from "../src/cli";

const python = process.env.VKM_TEST_PYTHON ?? "python3";
const available = process.platform !== "win32"
  && spawnSync(python, ["-c", "import virtual_key_mapper.auth.forwarded"]).status === 0;
// CI lists "relay" in VKM_REQUIRE on Linux and macOS: there a skip would hide a broken setup.
const required = (process.env.VKM_REQUIRE ?? "").split(",").map((item) => item.trim()).includes("relay");

test("the relay test runs where CI requires it", { skip: !required }, () => {
  assert.ok(available, `VKM_REQUIRE requires relay, but ${python} cannot import the CLI or this is Windows`);
});

test("the CLI's forwarded sign-in gets its token from the relay", { skip: !available && "needs Linux or macOS and the CLI" }, async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "vkm-interop-"));
  const socket = path.join(directory, "relay", "agent.sock");
  const home = path.join(directory, "home");
  const seen: unknown[] = [];
  const server = await serve(socket, async (request) => {
    seen.push(request);
    return { access_token: "relayed-token", expires_at: null, profile: "work", user: "me" };
  });
  try {
    const env = { ...process.env, VKM_AGENT: socket };
    const cli = (...args: string[]) => new Promise<string>((resolve, reject) => {
      // Asynchronous: the relay answers on this process's event loop.
      execFile(python, ["-m", "virtual_key_mapper", "--home", home, ...args], { env },
        (error, stdout, stderr) => (error ? reject(new Error(stderr)) : resolve(stdout)));
    });
    // The profile as the extension creates it from the local one: one call, with the local profile's own definitions.
    const labels = [{ key: "customer", required: true, description: "-Who pays", values: [{ id: "acme", name: "Acme" }], strict: true }];
    const outputs = { tags: { format: ["${customer}"] } };
    const local = { name: "work", gateway: { type: "litellm", url: "https://gateway.example" }, rules: null, labels, outputs };
    await cli(...remoteProfileArgs(local).args);
    assert.equal(await cli("token"), "relayed-token\n");
    assert.deepEqual(seen, [{ operation: "token", profile: "work" }]);
    const shown = JSON.parse(await cli("profile", "show", "--json"));
    assert.deepEqual([shown.labels, shown.outputs, shown.auth], [labels, outputs, { type: "forwarded", host_profile: "work" }]);
  } finally {
    server.close();
    execFileSync("rm", ["-rf", directory]);
  }
});
