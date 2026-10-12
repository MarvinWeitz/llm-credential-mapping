import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { Busy, agentPath, parseRequest, serve, stillServing, type AgentRequest } from "../src/agent";

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

/** A socket file nobody listens on, as a crashed window leaves it. */
async function staleSocket(socket: string): Promise<void> {
  const first = await serve(socket, async () => ({}));
  fs.linkSync(socket, `${socket}.keep`);
  await new Promise<void>((resolve) => first.close(() => resolve()));
  fs.renameSync(`${socket}.keep`, socket);
}

const close = (server: net.Server) => new Promise<void>((resolve) => server.close(() => resolve()));

test("a stale socket is replaced and the socket is private", { skip: windows }, async () => {
  const socket = socketPath("stale");
  await staleSocket(socket);
  const second = await serve(socket, async () => ({ ok: 1 }));
  try {
    assert.equal(fs.statSync(socket).mode & 0o777, 0o600);
    assert.equal(fs.statSync(path.dirname(socket)).mode & 0o777, 0o700);
    assert.deepEqual(await ask(socket, '{"version":1,"operation":"token","profile":""}\n'), { ok: true, result: { ok: 1 } });
  } finally {
    await close(second);
  }
  assert.equal(fs.existsSync(socket), false, "a closing window removes its socket");
});

test("a file that is not a socket is never deleted", { skip: windows }, async () => {
  const socket = socketPath("file");
  fs.mkdirSync(path.dirname(socket), { recursive: true, mode: 0o700 });
  fs.writeFileSync(socket, "keep me");
  await assert.rejects(serve(socket, async () => ({})), /not a socket of yours/);
  assert.equal(fs.readFileSync(socket, "utf8"), "keep me");
});

test("of two windows racing for a stale socket, one serves and the other notices", { skip: windows }, async () => {
  const socket = socketPath("race");
  await staleSocket(socket);
  const started = await Promise.allSettled([serve(socket, async () => ({ w: "A" })), serve(socket, async () => ({ w: "B" }))]);
  const servers = started.flatMap((result) => (result.status === "fulfilled" ? [result.value] : []));
  const [owner, ...lost] = [...servers.filter(stillServing), ...servers.filter((server) => !stillServing(server))];
  assert.equal(servers.filter(stillServing).length, 1);
  for (const server of lost) await close(server); // What the extension's watchdog does.
  assert.equal((await ask(socket, '{"version":1,"operation":"token","profile":""}\n')).ok, true, "closing the loser keeps the socket");
  await close(owner);
  assert.equal(fs.existsSync(socket), false);
});

test("a directory others can enter is refused", { skip: windows }, async () => {
  const socket = socketPath("open");
  fs.mkdirSync(path.dirname(socket), { recursive: true });
  fs.chmodSync(path.dirname(socket), 0o777);
  await assert.rejects(serve(socket, async () => ({})), /must belong to you and be private/);
});
