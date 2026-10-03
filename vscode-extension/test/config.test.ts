import { strict as assert } from "node:assert";
import { exec } from "node:child_process";
import * as path from "node:path";
import { promisify } from "node:util";
import { test } from "node:test";
import { parse } from "jsonc-parser";
import { Cli } from "../src/cli";
import { configureClaudeDocument, helperCommand, mergeClaudeSettings, sameProfile, validateClaudeEnvironment } from "../src/claude";

const profile = { name: "fixture-a", gateway: "https://gateway.example.test", team_id: "a", allow_http: false };

test("Claude settings merge preserves unrelated settings, clears competing auth and never copies tokens", () => {
  const value = mergeClaudeSettings({
    permissions: { allow: ["Read"] }, env: { CUSTOM: "keep", ANTHROPIC_API_KEY: "old-secret" },
  }, profile, "helper token --project fixture-a", "claude-alias");
  assert.deepEqual(value.permissions, { allow: ["Read"] });
  assert.equal(JSON.stringify(value).includes("old-secret"), false);
  assert.deepEqual(value.env, {
    CUSTOM: "keep", ANTHROPIC_API_KEY: "", ANTHROPIC_AUTH_TOKEN: "",
    ANTHROPIC_BASE_URL: profile.gateway, CLAUDE_CODE_USE_BEDROCK: "",
    CLAUDE_CODE_USE_VERTEX: "", CLAUDE_CODE_USE_FOUNDRY: "",
    ANTHROPIC_MODEL: "claude-alias", ANTHROPIC_DEFAULT_OPUS_MODEL: "claude-alias",
    ANTHROPIC_DEFAULT_SONNET_MODEL: "claude-alias", ANTHROPIC_DEFAULT_HAIKU_MODEL: "claude-alias",
    CLAUDE_CODE_SUBAGENT_MODEL: "claude-alias", CLAUDE_CODE_API_KEY_HELPER_TTL_MS: "30000",
  });
});

test("JSONC comments, trailing commas and unrelated env formatting survive", () => {
  const text = '{\n  // keep permissions\n  "permissions": {"allow": ["Read"]},\n  "env": {\n    // keep custom entry\n    "CUSTOM": "yes",\n  },\n}\n';
  const updated = configureClaudeDocument(text, profile, "helper", "alias");
  assert.match(updated, /keep permissions/);
  assert.match(updated, /keep custom entry/);
  const parsed = parse(updated);
  assert.equal(parsed.env.CUSTOM, "yes");
  assert.equal(parsed.apiKeyHelper, "helper");
  assert.equal(parsed.env.ANTHROPIC_BASE_URL, profile.gateway);
});

test("invalid settings are rejected rather than overwritten", () => {
  for (const value of ["{", "[]", '{"env":[]}']) assert.throws(() => configureClaudeDocument(value, profile, "helper", "alias"), /invalid|object/);
  assert.throws(() => mergeClaudeSettings({}, profile, "helper", ""), /alias/);
});

test("credential helper runs on the actual OS with shell-safe literal arguments", async () => {
  const cli = new Cli({
    executable: process.execPath, args: [path.join(__dirname, "fixture.js")],
    home: "home with ' quotes & characters", dbusAddress: "",
  });
  const command = helperCommand(cli, profile.name);
  assert.doesNotMatch(command, /synthetic-token/);
  const result = await promisify(exec)(command, { timeout: 10_000, windowsHide: true });
  assert.equal(result.stdout.trim(), "synthetic-token-fixture-a");
});

test("POSIX helper carries the vault session; Windows helper contains no credentials", () => {
  const cli = new Cli({ executable: "/path with ' quote/python", args: ["-m", "llm_credential_mapping"], home: "", dbusAddress: "unix:path=/tmp/user bus" });
  const posix = helperCommand(cli, "fixture-a", "linux");
  assert.match(posix, /DBUS_SESSION_BUS_ADDRESS/);
  assert.match(posix, /'\\''/);
  const windows = helperCommand(cli, "fixture-a", "win32");
  const script = Buffer.from(windows.split(" ").at(-1) ?? "", "base64").toString("utf16le");
  assert.match(script, /'token' '--project' 'fixture-a'/);
  assert.match(script, /exit \$LASTEXITCODE/);
});

test("workspace binding equality includes gateway and team, not just a display name", () => {
  assert.equal(sameProfile(profile, { ...profile }), true);
  assert.equal(sameProfile(profile, { ...profile, gateway: "https://other.test" }), false);
  assert.equal(sameProfile(profile, { ...profile, team_id: "other" }), false);
});

test("competing machine/environment credentials fail explicitly without revealing their values", () => {
  assert.throws(() => validateClaudeEnvironment([{ name: "ANTHROPIC_AUTH_TOKEN", value: "secret" }], {}, profile, "alias"), (error: Error) => {
    assert.match(error.message, /conflicting ANTHROPIC_AUTH_TOKEN/);
    assert.doesNotMatch(error.message, /secret/);
    return true;
  });
  assert.throws(() => validateClaudeEnvironment([], { ANTHROPIC_BASE_URL: "https://other.test" }, profile, "alias"), /BASE_URL/);
  assert.doesNotThrow(() => validateClaudeEnvironment([], { ANTHROPIC_BASE_URL: profile.gateway }, profile, "alias"));
  const updated = mergeClaudeSettings({ env: { ANTHROPIC_CUSTOM_HEADERS: "Authorization: Bearer old-secret" } }, profile, "helper", "alias");
  assert.doesNotMatch(JSON.stringify(updated), /old-secret/);
});

test("Windows helper fails rather than reports success for a missing executable", async () => {
  const cli = new Cli({ executable: "llm-missing-fixture-executable", args: [], home: "", dbusAddress: "" });
  await assert.rejects(promisify(exec)(helperCommand(cli, "fixture-a"), { timeout: 10_000, windowsHide: true }));
});
