// Print the native executables of the agents installed here (`npm ci`) as test environment variables:
//   VKM_TEST_CLAUDE_BINARY=...
// The tests call these directly instead of npm's shims: on Windows the shims are .cmd files, and
// cmd.exe would rewrite the JSON arguments the launcher passes.
import { execFileSync } from "node:child_process";
import { existsSync, readdirSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(import.meta.url);
const exe = process.platform === "win32" ? ".exe" : "";

function packageRoot(name) {
  return path.dirname(require.resolve(`${name}/package.json`));
}

function codex() {
  // The npm package is a Node launcher; the binary is in the platform package (see @openai/codex/bin/codex.js).
  const platform = { linux: "linux", darwin: "darwin", win32: "win32" }[process.platform];
  const vendor = path.join(packageRoot(`@openai/codex-${platform}-${process.arch}`), "vendor");
  for (const triple of readdirSync(vendor)) {
    const candidate = path.join(vendor, triple, "bin", `codex${exe}`);
    if (existsSync(candidate)) return candidate;
  }
  throw new Error(`No Codex binary under ${vendor}`);
}

const binaries = {
  CLAUDE: path.join(packageRoot("@anthropic-ai/claude-code"), "bin", "claude.exe"),
  CODEX: codex(),
  OPENCODE: path.join(packageRoot("opencode-ai"), "bin", "opencode.exe"),
};

for (const [name, binary] of Object.entries(binaries)) {
  if (!existsSync(binary)) throw new Error(`Missing ${name} binary: ${binary}`);
  const version = execFileSync(binary, ["--version"], { encoding: "utf8", timeout: 60_000 }).trim();
  process.stderr.write(`${name.toLowerCase()}: ${version} (${binary})\n`);
  process.stdout.write(`VKM_TEST_${name}_BINARY=${binary}\n`);
}
