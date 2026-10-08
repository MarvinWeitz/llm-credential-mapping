import assert from "node:assert/strict";
import { test } from "node:test";
import { answer, RunError } from "../src/handler";

test("token requests run the local CLI and pass on only the token fields", async () => {
  const calls: string[][] = [];
  const result = await answer({ operation: "token", profile: "work" }, async (args) => {
    calls.push(args);
    return JSON.stringify({ access_token: "local-token", expires_at: 99, profile: "work", user: "me", extra: "dropped" });
  }, "default");
  assert.deepEqual(calls, [["token", "--json", "--profile", "work"]]);
  assert.deepEqual(result, { ok: true, result: { access_token: "local-token", expires_at: 99, profile: "work", user: "me" } });
});

test("profile requests return the nonsecret description, using the default profile when none is named", async () => {
  const calls: string[][] = [];
  const result = await answer({ operation: "profile", profile: "" }, async (args) => {
    calls.push(args);
    return JSON.stringify({ name: "work", gateway: { type: "litellm", url: "https://g.example" }, auth: { type: "litellm-sso", team_id: "t" }, endpoints: {} });
  }, "work");
  assert.deepEqual(calls, [["profile", "show", "work", "--json"]]);
  assert.deepEqual(result, { ok: true, result: { name: "work", gateway: { type: "litellm", url: "https://g.example" }, endpoints: {} } });
});

test("anything else is refused without running the CLI", async () => {
  for (const request of [null, "token", { operation: "login", profile: "" }, { operation: "token", profile: "--home x" }, { operation: "token" }]) {
    const result = await answer(request, async () => assert.fail("must not run"), "");
    assert.equal(result.ok, false);
  }
});

test("CLI errors come back with a sign-in hint when a login is needed", async () => {
  const result = await answer({ operation: "token", profile: "" }, async () => {
    throw new RunError("No login for 'work'; run 'llm-credentials login --profile work'.");
  }, "");
  assert.deepEqual(result, { ok: false, error: "No login for 'work'; run 'llm-credentials login --profile work'.", login: true });
  const unreadable = await answer({ operation: "token", profile: "" }, async () => "not json", "");
  assert.equal(unreadable.ok === false && unreadable.login, false);
});
