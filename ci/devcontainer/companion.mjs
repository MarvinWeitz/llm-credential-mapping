// The local machine's side of the dev container test: the companion extension's request handler
// (vscode-extension/local/src/handler.ts, unchanged) answering with the host CLI and its OS vault.
// In VS Code the relay reaches it through a VS Code command; here, through HTTP on the shared host network.
//
//   LLMC_HOST_CLI='["python","-m","llm_credential_mapping","--home","/path"]' node companion.mjs PORT
import { execFile } from "node:child_process";
import http from "node:http";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { answer, RunError } = require("../../vscode-extension/local/out/src/handler.js");

const command = JSON.parse(process.env.LLMC_HOST_CLI);
// Like the extension's runner: stderr of a failing command becomes the error message.
const run = (args) => new Promise((resolve, reject) => {
  execFile(command[0], [...command.slice(1), ...args], { maxBuffer: 4 << 20, timeout: 60000 }, (error, stdout, stderr) => {
    if (!error) return resolve(stdout);
    reject(new RunError(String(stderr || error.message).replace(/^Error: /, "").trim()));
  });
});

http.createServer((request, response) => {
  let raw = "";
  request.on("data", (chunk) => (raw += chunk));
  request.on("end", async () => {
    let body;
    try {
      body = JSON.parse(raw);
    } catch {
      return response.writeHead(400).end(); // Readiness probes and anything that is not a relay request.
    }
    const result = await answer(body, run, []); // llmCredentials.localProfiles unset: the local default only.
    console.log(`companion: ${body?.operation} -> ${result.ok ? "ok" : result.error}`);
    response.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(result));
  });
}).listen(Number(process.argv[2]), "127.0.0.1", () => console.log(`companion listening on ${process.argv[2]}`));
