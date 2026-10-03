import { strict as assert } from "node:assert";
import { test } from "node:test";
import * as path from "node:path";
import { Cli, parseProfiles } from "../src/cli";

function client(mode = "normal") {
  return new Cli({ executable: process.execPath, args: [path.join(__dirname, "fixture.js"), "--fixture=" + mode], home: "", dbusAddress: "" });
}

test("versioned project parsing and explicit token project", async () => {
  const cli = client();
  const profile = await cli.profile("fixture-b");
  assert.equal(profile.team_id, "team-fixture-b");
  assert.equal(await cli.token(profile), "synthetic-token-fixture-b");
});

test("unknown project does not fall back to global active selection", async () => {
  await assert.rejects(client().profile("missing"), /no longer configured/);
});

test("CLI failures never expose stdout/stderr secrets", async () => {
  await assert.rejects(client("fail").run(["token"]), (error: Error) => {
    assert.match(error.message, /CLI failed/);
    assert.doesNotMatch(error.message, /synthetic-secret/);
    return true;
  });
});

test("token output rejects banners and multiline output", async () => {
  await assert.rejects(client("bad-token").token({ name: "fixture-a", gateway: "https://example.test", team_id: "a", allow_http: false }), /only stdout/);
});

test("successful CLI warnings are surfaced without revealing raw output", async () => {
  const warnings: string[] = [];
  const cli = new Cli(client("warn").runtime, (message) => warnings.push(message));
  await cli.token({ name: "fixture-a", gateway: "https://example.test", team_id: "a", allow_http: false });
  assert.equal(warnings.length, 1);
  assert.match(warnings[0] ?? "", /reported a warning/);
  assert.doesNotMatch(warnings[0] ?? "", /synthetic-sensitive/);
});

test("CLI cancellation and output limits", async () => {
  const controller = new AbortController();
  const promise = client("hang").run(["token"], controller.signal);
  controller.abort();
  await assert.rejects(promise, /cancelled/);
  await assert.rejects(client("oversize").run(["token"]), /exceeded/);
});

test("home is a literal global argument before the subcommand", async () => {
  const cli = client("args");
  cli.runtime.home = "directory with ' quotes & characters";
  const args: unknown = JSON.parse(await cli.run(["token", "--project", "fixture-a"]));
  assert.deepEqual(args, ["--home", cli.runtime.home, "token", "--project", "fixture-a"]);
});

test("gateway metadata refuses credentials, redirects-as-origins and remote HTTP", () => {
  for (const gateway of ["http://remote.test", "https://user:password@example.test", "https://example.test/v1", "https://example.test?key=secret", "ftp://localhost"]) {
    assert.throws(() => parseProfiles({ version: 1, projects: [{ name: "a", gateway, team_id: "a", allow_http: true }] }), /gateway/);
  }
  assert.equal(parseProfiles({ version: 1, projects: [{ name: "a", gateway: "http://127.0.0.1:1234", team_id: "a", allow_http: true }] })[0]?.gateway, "http://127.0.0.1:1234");
  assert.throws(() => parseProfiles({ version: 2, projects: [] }), /Update/);
});
