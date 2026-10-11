// The relay inside a remote window (dev container, SSH, WSL): a private local socket on which the CLI's
// "forwarded" sign-in asks for credentials. Each request is passed to the local machine through VS Code.
// Protocol (one JSON line per connection), shared with the CLI's auth/forwarded.py:
//   {"version": 1, "operation": "token" | "profile", "profile": NAME}  ->  {"ok": true, "result": {...}} | {"ok": false, "error": "..."}
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";

export const AGENT_VARIABLE = "LLM_CREDENTIALS_AGENT";
export const OPERATIONS = ["token", "profile"] as const;
const MAX_REQUEST = 65536;

export interface AgentRequest {
  operation: (typeof OPERATIONS)[number];
  profile: string;
}

/** Answers a request with a result object or throws an Error whose message goes back to the CLI. */
export type Forward = (request: AgentRequest) => Promise<unknown>;

/** The socket path; must match agent_path() in the CLI. */
export function agentPath(env: NodeJS.ProcessEnv = process.env): string {
  if (env[AGENT_VARIABLE]) return env[AGENT_VARIABLE]!;
  if (env.XDG_RUNTIME_DIR) return path.join(env.XDG_RUNTIME_DIR, "llm-credentials", "agent.sock");
  const user = typeof process.getuid === "function" ? process.getuid() : os.userInfo().username;
  return path.join(os.tmpdir(), `llm-credentials-${user}`, "agent.sock");
}

export function parseRequest(raw: string): AgentRequest {
  let value: any;
  try {
    value = JSON.parse(raw);
  } catch {
    throw new Error("Invalid request.");
  }
  if (
    value?.version !== 1 || !OPERATIONS.includes(value.operation) || typeof value.profile !== "string"
    || !/^([A-Za-z0-9][A-Za-z0-9._-]{0,63})?$/.test(value.profile)
  ) {
    throw new Error("Unsupported request; update the llm-credentials CLI and the VS Code extensions together.");
  }
  return { operation: value.operation, profile: value.profile };
}

function handle(connection: net.Socket, forward: Forward): void {
  let raw = "";
  connection.setEncoding("utf8");
  connection.setTimeout(150000, () => connection.destroy());
  const reply = (answer: object) => connection.end(JSON.stringify(answer) + "\n");
  connection.on("error", () => undefined);
  connection.on("data", (chunk: string) => {
    raw += chunk;
    if (raw.length > MAX_REQUEST) return reply({ ok: false, error: "Request too large." });
    if (!raw.includes("\n")) return;
    connection.removeAllListeners("data");
    let request: AgentRequest;
    try {
      request = parseRequest(raw.slice(0, raw.indexOf("\n")));
    } catch (error) {
      return reply({ ok: false, error: (error as Error).message });
    }
    forward(request).then(
      (result) => reply({ ok: true, result }),
      (error: unknown) => reply({ ok: false, error: error instanceof Error ? error.message : String(error) }),
    );
  });
}

const isPipe = (socketPath: string) => socketPath.startsWith("\\\\.\\pipe\\");

/** Create the socket's directory, or accept it only if it belongs to us and nobody else can enter it. */
function privateDirectory(directory: string): void {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const info = fs.statSync(directory);
  if (typeof process.getuid === "function" && (info.uid !== process.getuid() || (info.mode & 0o077) !== 0)) {
    throw new Error(`Refusing to listen in ${directory}: it must belong to you and be private (chmod 700).`);
  }
}

/** True when another relay (e.g. another VS Code window on the same container) already answers on the socket. */
function answers(socketPath: string): Promise<boolean> {
  return new Promise((resolve) => {
    const probe = net.connect(socketPath);
    probe.once("connect", () => {
      probe.destroy();
      resolve(true);
    });
    probe.once("error", () => resolve(false));
  });
}

export class Busy extends Error {}

/** Listen on ``socketPath``; throws Busy when another relay is already serving it. */
export async function serve(socketPath: string, forward: Forward): Promise<net.Server> {
  if (!isPipe(socketPath)) {
    privateDirectory(path.dirname(socketPath));
    if (fs.existsSync(socketPath)) {
      if (await answers(socketPath)) throw new Busy(`Another VS Code window serves ${socketPath}.`);
      fs.unlinkSync(socketPath); // Left behind by a closed window.
    }
  }
  const server = net.createServer((connection) => handle(connection, forward));
  await new Promise<void>((resolve, reject) => {
    server.once("error", (error: NodeJS.ErrnoException) =>
      reject(error.code === "EADDRINUSE" ? new Busy(`Another VS Code window serves ${socketPath}.`) : error));
    server.listen(socketPath, () => resolve());
  });
  if (!isPipe(socketPath)) fs.chmodSync(socketPath, 0o600);
  return server;
}
