import assert from "node:assert/strict";
import { test } from "node:test";
import type { CliLabel, CliLabels, CliToken } from "../src/cli";
import { TokenCache } from "../src/credentials";
import { choose, LabelCache, summary, tooltip } from "../src/labels";

const tokenAt = (value: string, expires_at: number | null): CliToken => ({ access_token: value, expires_at, profile: "p", user: null });

test("tokens are cached until shortly before expiry, at most four minutes, and shared by concurrent calls", async () => {
  let now = 1000;
  let calls = 0;
  const cache = new TokenCache(async () => tokenAt(`t${++calls}`, now + 600), () => now);
  assert.deepEqual(await Promise.all([cache.get(), cache.get()]), ["t1", "t1"]);
  now += 239;
  assert.equal(await cache.get(), "t1");
  now += 2;
  assert.equal(await cache.get(), "t2");
  cache.invalidate();
  assert.equal(await cache.get(), "t3");
});

test("tokens close to expiry are renewed sooner, and keys without expiry use the four-minute limit", async () => {
  let now = 1000;
  let calls = 0;
  const expiring = new TokenCache(async () => tokenAt(`t${++calls}`, now + 90), () => now);
  await expiring.get();
  now += 31;
  assert.equal(await expiring.get(), "t2");
  const fixed = new TokenCache(async () => tokenAt("key", null), () => now);
  assert.equal(await fixed.get(), "key");
});

test("failed token calls are not cached", async () => {
  let fail = true;
  const cache = new TokenCache(async () => {
    if (fail) throw new Error("No login");
    return tokenAt("t", null);
  });
  await assert.rejects(cache.get(), /No login/);
  fail = false;
  assert.equal(await cache.get(), "t");
});

const label = (key: string, value: string | null, extra: Partial<CliLabel> = {}): CliLabel =>
  ({ key, value, name: null, source: value === null ? null : "file", detail: value === null ? null : "/r/.vkm", required: false, description: null, ...extra });

const labels = (values: Record<string, string | null>, extra: Partial<CliLabels> = {}): CliLabels => ({
  labels: Object.entries(values).map(([key, value]) => label(key, value)), missing: [], ignored: [], repository: null,
  complete: true, headers: { "x-test-tags": JSON.stringify(values) }, message: null, notices: [], ...extra,
});

test("the folder is the only one, the active editor's, the chosen one, or the first of folders with equal label values", async () => {
  const folders: Record<string, CliLabels> = {
    "/a": labels({ customer: "acme", project: null }, { repository: "github.com/acme/a" }),
    "/b": labels({ customer: "acme", project: null }, { repository: "github.com/acme/b" }),
    "/c": labels({ customer: "acme", project: "web" }),
    "/m": labels({ customer: null }, { missing: ["customer"], complete: false, headers: {}, message: "Required labels have no value: customer" }),
  };
  const cache = new LabelCache(async (folder) => {
    if (!folders[folder]) throw new Error(`No profile for ${folder}`);
    return folders[folder];
  });
  assert.deepEqual(await choose([], undefined, undefined, cache), { kind: "none" });
  assert.equal((await choose(["/c"], undefined, undefined, cache) as any).folder, "/c");
  assert.equal((await choose(["/a", "/c"], "/c", "/a", cache) as any).folder, "/c");
  assert.equal((await choose(["/a", "/c"], undefined, "/a", cache) as any).folder, "/a");
  // Different repositories (and so different metadata headers), the same values: the first folder.
  assert.equal((await choose(["/a", "/b"], undefined, undefined, cache) as any).folder, "/a");
  assert.equal((await choose(["/a", "/c"], undefined, undefined, cache)).kind, "choose", "an optional label differs");
  assert.equal((await choose(["/a", "/m"], undefined, undefined, cache)).kind, "choose");
  const missing = await choose(["/m"], undefined, undefined, cache) as any;
  assert.deepEqual([missing.kind, missing.labels.complete], ["folder", false], "the caller blocks it");
  const ambiguous = await choose(["/a", "/b", "/d"], undefined, undefined, cache);
  assert.equal(ambiguous.kind, "choose");
  assert.match((ambiguous as any).folders[2].error.message, /No profile/);
  await assert.rejects(choose(["/d"], undefined, undefined, cache), /No profile/);
});

test("label answers are cached briefly and failures are retried", async () => {
  let now = 0;
  let calls = 0;
  const cache = new LabelCache(async () => {
    calls += 1;
    if (calls === 1) throw new Error("offline");
    return labels({ customer: `c${calls}` });
  }, () => now);
  await assert.rejects(cache.get("/a"), /offline/);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal((await cache.get("/a")).labels[0].value, "c2");
  now += 29999;
  assert.equal((await cache.get("/a")).labels[0].value, "c2");
  now += 2;
  assert.equal((await cache.get("/a")).labels[0].value, "c3");
});

test("the status bar shows the values in order by their names, and the missing required labels as a warning", () => {
  const shown = labels({});
  shown.labels = [
    label("customer", "acme", { name: "Acme Corp", required: true, description: "Who pays", source: "rules", detail: "rule ** matched x" }),
    label("team", null),
    label("project", "web"),
  ];
  assert.deepEqual(summary(shown), { text: "Acme Corp · web", warning: false });
  const text = tooltip(shown, "/r");
  assert.match(text, /customer: Acme Corp \(acme\) · Who pays\n {4}rules: rule \*\* matched x/);
  assert.match(text, /team: no value\n/);
  assert.match(text, /project: web\n {4}file: \/r\/\.vkm/);
  const missing = labels({ customer: null, project: "web" }, {
    missing: ["customer"], complete: false, headers: {}, message: "Required labels have no value: customer for folder /r.",
    ignored: [{ key: "tema", value: "x", source: "file", detail: "/r/.vkm", reason: "unknown label" }],
  });
  missing.labels[0].required = true;
  assert.deepEqual(summary(missing), { text: "web · missing customer", warning: true });
  assert.match(tooltip(missing, "/r"), /customer: no value \(required\)[^]*Ignored tema from \/r\/\.vkm: unknown label\nRequired labels have no value/);
  assert.deepEqual(summary(labels({ customer: "acme" }, { complete: false, headers: {}, message: "too long" })),
    { text: "acme · labels unusable", warning: true });
});

test("icon syntax in names, values and missing keys is shown as typed, not as an icon", () => {
  const shown = labels({ customer: "$(alert)acme", project: "web" }, { missing: ["$(x)", "team"], complete: false, headers: {} });
  shown.labels[1].name = "$(sync~spin) Web";
  assert.deepEqual(summary(shown), { text: "\\$(alert)acme · \\$(sync~spin) Web · missing \\$(x), team", warning: true });
});

test("without label definitions the status bar says 'no labels' and requests carry only the repository", () => {
  const none = labels({}, { message: "No labels are defined for this profile; requests carry only the repository." });
  assert.deepEqual(summary(none), { text: "no labels", warning: false });
  assert.match(tooltip(none, "/r"), /No labels are defined/);
});
