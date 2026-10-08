import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { Busy, agentPath, parseRequest, serve, type AgentRequest } from "../src/agent";

const windows = process.platform === "win32";

function socketPath(name: string): string {
  return windows
    ? `\\\\.\\pipe\\llm-credentials-test-${process.pid}-${name}`
    : path.join(fs.mkdtempSync(path.join(os.tmpdir(), "llmc-")), "private", "agent.sock");
}

function ask(socket: string, line: string): Promise<any> {
  return new Promise((resolve, reject) => {
    let raw = "";
    const connection = net.connect(socket, () => connection.write(line));
    connection.setEncoding("utf8");
    connection.on("data", (chunk) => (raw += chunk));
    connection.on("end", () => resolve(JSON.parse(raw)));
    connection.on("error", reject);
  });
}

test("the socket path matches the CLI's", () => {
  assert.equal(agentPath({ LLM_CREDENTIALS_AGENT: "/run/x.sock" }), "/run/x.sock");
  assert.equal(agentPath({ XDG_RUNTIME_DIR: "/run/user/1000" }), path.join("/run/user/1000", "llm-credentials", "agent.sock"));
  assert.match(agentPath({}), /llm-credentials-.+[\\/]agent\.sock$/);
});

test("only known operations and profile names are accepted", () => {
  assert.deepEqual(parseRequest('{"version":1,"operation":"token","profile":""}'), { operation: "token", profile: "" });
  for (const raw of ["nope", '{"version":2,"operation":"token","profile":""}', '{"version":1,"operation":"login","profile":""}',
    '{"version":1,"operation":"token","profile":"../x"}', '{"version":1,"operation":"token"}']) {
    assert.throws(() => parseRequest(raw), raw);
  }
});

test("requests are relayed and answered, failures come back as errors", async () => {
  const seen: AgentRequest[] = [];
  const socket = socketPath("relay");
  const server = await serve(socket, async (request) => {
    seen.push(request);
    if (request.profile === "missing") throw new Error("No login for 'missing'.");
    return { access_token: "relayed", expires_at: null };
  });
  try {
    assert.deepEqual(await ask(socket, '{"version":1,"operation":"token","profile":"work"}\n'),
      { ok: true, result: { access_token: "relayed", expires_at: null } });
    assert.deepEqual(await ask(socket, '{"version":1,"operation":"token","profile":"missing"}\n'),
      { ok: false, error: "No login for 'missing'." });
    assert.equal((await ask(socket, '{"version":1,"operation":"logout","profile":""}\n')).ok, false);
    assert.deepEqual(seen.map((request) => request.profile), ["work", "missing"]);
  } finally {
    server.close();
  }
});

test("a second window does not take over a served socket", async () => {
  const socket = socketPath("busy");
  const server = await serve(socket, async () => ({}));
  try {
    await assert.rejects(serve(socket, async () => ({})), Busy);
  } finally {
    server.close();
  }
});

test("a stale socket is replaced and the socket is private", { skip: windows }, async () => {
  const socket = socketPath("stale");
  const first = await serve(socket, async () => ({}));
  await new Promise<void>((resolve) => first.close(() => resolve()));
  fs.writeFileSync(socket, ""); // A leftover file nobody listens on.
  const second = await serve(socket, async () => ({ ok: 1 }));
  try {
    assert.equal(fs.statSync(socket).mode & 0o777, 0o600);
    assert.equal(fs.statSync(path.dirname(socket)).mode & 0o777, 0o700);
  } finally {
    second.close();
  }
});

test("a directory others can enter is refused", { skip: windows }, async () => {
  const socket = socketPath("open");
  fs.mkdirSync(path.dirname(socket), { recursive: true });
  fs.chmodSync(path.dirname(socket), 0o777);
  await assert.rejects(serve(socket, async () => ({})), /must belong to you and be private/);
});
