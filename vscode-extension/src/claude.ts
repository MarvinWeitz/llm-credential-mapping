import { Cli, IntegrationError, Profile, record } from "./cli";
import { applyEdits, modify, parse, ParseError } from "jsonc-parser";

function posix(value: string): string { return "'" + value.replace(/'/g, "'\\''") + "'"; }

export function helperCommand(cli: Cli, project: string, platform: NodeJS.Platform = process.platform): string {
  const args = cli.arguments(["token", "--project", project]);
  if (platform === "win32") {
    const quoted = [cli.runtime.executable, ...args].map((item) => "'" + item.replace(/'/g, "''") + "'").join(" ");
    const script = `$ErrorActionPreference = 'Stop'; & ${quoted}; exit $LASTEXITCODE`;
    return "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + Buffer.from(script, "utf16le").toString("base64");
  }
  return [
    ...(cli.runtime.dbusAddress ? ["env", posix("DBUS_SESSION_BUS_ADDRESS=" + cli.runtime.dbusAddress)] : []),
    posix(cli.runtime.executable), ...args.map(posix),
  ].join(" ");
}

export function mergeClaudeSettings(value: unknown, profile: Profile, helper: string, alias: string): Record<string, unknown> {
  if (!record(value) || (value.env !== undefined && !record(value.env))) {
    throw new IntegrationError("Claude settings.local.json must contain an object with an optional env object.");
  }
  if (!alias.trim() || /[\x00-\x1f]/.test(alias)) throw new IntegrationError("Provide an exact LiteLLM Claude model alias.");
  return {
    ...value, apiKeyHelper: helper,
    env: {
      ...(record(value.env) ? value.env : {}),
      ...(record(value.env) && typeof value.env.ANTHROPIC_CUSTOM_HEADERS === "string"
        && /(^|\n)\s*(authorization|x-api-key)\s*:/i.test(value.env.ANTHROPIC_CUSTOM_HEADERS)
        ? { ANTHROPIC_CUSTOM_HEADERS: "" } : {}),
      ANTHROPIC_BASE_URL: profile.gateway,
      ANTHROPIC_API_KEY: "",
      ANTHROPIC_AUTH_TOKEN: "",
      CLAUDE_CODE_USE_BEDROCK: "",
      CLAUDE_CODE_USE_VERTEX: "",
      CLAUDE_CODE_USE_FOUNDRY: "",
      ANTHROPIC_MODEL: alias,
      ANTHROPIC_DEFAULT_OPUS_MODEL: alias,
      ANTHROPIC_DEFAULT_SONNET_MODEL: alias,
      ANTHROPIC_DEFAULT_HAIKU_MODEL: alias,
      CLAUDE_CODE_SUBAGENT_MODEL: alias,
      CLAUDE_CODE_API_KEY_HELPER_TTL_MS: "30000",
    },
  };
}

export function validateClaudeEnvironment(value: unknown, inherited: NodeJS.ProcessEnv, profile: Profile, alias: string): void {
  if (!Array.isArray(value) || value.some((item: unknown) => !record(item)
    || typeof item.name !== "string" || typeof item.value !== "string")) {
    throw new IntegrationError("Claude environmentVariables must be an array of name/value entries.");
  }
  const entries = new Map<string, string | undefined>(Object.entries(inherited));
  for (const item of value) {
    if (record(item) && typeof item.name === "string" && typeof item.value === "string") entries.set(item.name, item.value);
  }
  for (const name of ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"]) {
    if (entries.get(name)) throw new IntegrationError(`Remove conflicting ${name} from the host/Claude extension environment before configuring the project helper.`);
  }
  const gateway = entries.get("ANTHROPIC_BASE_URL");
  if (gateway && gateway.replace(/\/$/, "") !== profile.gateway) throw new IntegrationError("Remove the conflicting Claude extension/host ANTHROPIC_BASE_URL first.");
  const model = entries.get("ANTHROPIC_MODEL");
  if (model && model !== alias) throw new IntegrationError("Remove the conflicting Claude extension/host ANTHROPIC_MODEL first.");
  if (/(^|\n)\s*(authorization|x-api-key)\s*:/i.test(entries.get("ANTHROPIC_CUSTOM_HEADERS") ?? "")) {
    throw new IntegrationError("Remove authentication headers from ANTHROPIC_CUSTOM_HEADERS before using the project helper.");
  }
}

export function sameProfile(left: Profile, right: Profile): boolean {
  return left.name === right.name && left.gateway === right.gateway && left.team_id === right.team_id;
}

export function configureClaudeDocument(text: string, profile: Profile, helper: string, alias: string): string {
  const errors: ParseError[] = [];
  const current: unknown = parse(text || "{}", errors, { allowTrailingComma: true });
  if (errors.length) throw new IntegrationError("Claude settings.local.json contains invalid JSON/JSONC. Repair it before configuring.");
  const updated = mergeClaudeSettings(current, profile, helper, alias);
  let document = text || "{}\n";
  const options = { formattingOptions: { insertSpaces: true, tabSize: 2, eol: text.includes("\r\n") ? "\r\n" : "\n" } };
  document = applyEdits(document, modify(document, ["apiKeyHelper"], updated.apiKeyHelper, options));
  if (!record(updated.env)) throw new IntegrationError("Invalid generated Claude environment.");
  for (const [name, value] of Object.entries(updated.env)) {
    if (record(current) && record(current.env) && current.env[name] === value) continue;
    document = applyEdits(document, modify(document, ["env", name], value, options));
  }
  return document;
}
