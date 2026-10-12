import assert from "node:assert/strict";
import { test } from "node:test";
import { answer, RunError } from "../src/handler";

const WORK = JSON.stringify({ access_token: "local-token", expires_at: 99, profile: "work", user: "me", extra: "dropped" });

test("token requests run the local CLI and pass on only the token fields", async () => {
  const calls: string[][] = [];
  const result = await answer({ operation: "token", profile: "work" }, async (args) => {
    calls.push(args);
    return WORK;
  }, ["work"]);
  assert.deepEqual(calls, [["token", "--json", "--profile", "work"]]);
  assert.deepEqual(result, { ok: true, result: { access_token: "local-token", expires_at: 99, profile: "work", user: "me" } });
});

test("profile requests return the nonsecret description, using the first allowed profile when none is named", async () => {
  const calls: string[][] = [];
  const result = await answer({ operation: "profile", profile: "" }, async (args) => {
    calls.push(args);
    return JSON.stringify({ name: "work", gateway: { type: "litellm", url: "https://g.example" }, auth: { type: "litellm-sso", team_id: "t" }, endpoints: {} });
  }, ["work", "other"]);
  assert.deepEqual(calls, [["profile", "show", "work", "--json"]]);
  assert.deepEqual(result, { ok: true, result: { name: "work", gateway: { type: "litellm", url: "https://g.example" }, endpoints: {} } });
});

test("only the allowed local profiles are relayed, by default only the local default one", async () => {
  const calls: string[][] = [];
  const run = async (args: string[]) => {
    calls.push(args);
    return WORK; // The local default is "work".
  };
  const denied = await answer({ operation: "token", profile: "prod-admin" }, run, ["work"]);
  assert.equal(denied.ok, false);
  assert.match(!denied.ok ? denied.error : "", /not relayed.*localProfiles/);
  assert.deepEqual(calls, []);
  // Without a list, the CLI's default is asked for without --profile and only its own name passes.
  assert.equal((await answer({ operation: "token", profile: "work" }, run, [])).ok, true);
  assert.equal((await answer({ operation: "token", profile: "" }, run, [])).ok, true);
  const other = await answer({ operation: "token", profile: "prod-admin" }, run, []);
  assert.equal(other.ok, false);
  assert.deepEqual(calls, [["token", "--json"], ["token", "--json"], ["token", "--json"]]);
  const shown = await answer({ operation: "profile", profile: "prod-admin" }, async () => JSON.stringify({ name: "work", gateway: {} }), []);
  assert.equal(shown.ok, false);
});

test("anything else is refused without running the CLI", async () => {
  for (const request of [null, "token", { operation: "login", profile: "" }, { operation: "token", profile: "--home x" }, { operation: "token" }]) {
    const result = await answer(request, async () => assert.fail("must not run"), []);
    assert.equal(result.ok, false);
  }
});

test("CLI errors come back with a sign-in hint for the profile that needs a login", async () => {
  const result = await answer({ operation: "token", profile: "" }, async () => {
    throw new RunError("No login for 'work'; run 'llm-credentials login --profile work'.");
  }, []);
  assert.deepEqual(result, { ok: false, error: "No login for 'work'; run 'llm-credentials login --profile work'.", login: true, profile: "" });
  const named = await answer({ operation: "token", profile: "corp" }, async () => {
    throw new RunError("No login for 'corp'; run 'llm-credentials login --profile corp'.");
  }, ["dev", "corp"]);
  assert.equal(!named.ok && named.profile, "corp");
  const unreadable = await answer({ operation: "token", profile: "" }, async () => "not json", []);
  assert.equal(unreadable.ok === false && unreadable.login, false);
});
