// The dev container's side of the test: the main extension's relay (vscode-extension/src/agent.ts,
// unchanged) on the CLI's socket, forwarding to the companion like extension.ts's askLocal does,
// and creating the container's profile from the local one like ensureRemoteProfile does.
//
//   LLMC_COMPANION=http://127.0.0.1:PORT LLM_CREDENTIALS_AGENT=/path/agent.sock node relay.mjs
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { agentPath, serve } = require("../../vscode-extension/out/src/agent.js");

async function askLocal(request) {
  const response = await fetch(process.env.LLMC_COMPANION, { method: "POST", body: JSON.stringify(request) });
  const answer = await response.json();
  if (answer?.ok === true) return answer.result;
  throw new Error(answer?.error ?? "The local companion did not answer.");
}

const socketPath = agentPath();
await serve(socketPath, askLocal);
console.log(`relay listening on ${socketPath}`);

const local = await askLocal({ operation: "profile", profile: "" });
execFileSync("python", [
  "-m", "llm_credential_mapping", "profile", "add", local.name, "--gateway", local.gateway.url,
  "--gateway-type", local.gateway.type, "--auth", "forwarded", "--host-profile", local.name,
  ...(local.gateway.url.startsWith("http:") ? ["--allow-insecure-localhost"] : []),
], { stdio: "inherit" });
console.log(`relay: created profile '${local.name}' from the local one`);
