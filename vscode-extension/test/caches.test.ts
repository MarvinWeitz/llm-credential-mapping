import assert from "node:assert/strict";
import { test } from "node:test";
import { AttributionCache, choose } from "../src/attribution";
import type { CliAttribution, CliToken } from "../src/cli";
import { TokenCache } from "../src/credentials";

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

const customer = (name: string): CliAttribution => ({ customer: name, source: "file", detail: "", repository: null, headers: {} });

test("the folder is the only one, the active editor's, the chosen one, or one shared customer", async () => {
  const customers: Record<string, string> = { "/a": "acme", "/b": "acme", "/c": "other" };
  const cache = new AttributionCache(async (folder) => {
    if (!customers[folder]) throw new Error(`No customer is assigned to folder ${folder}`);
    return customer(customers[folder]);
  });
  assert.deepEqual(await choose([], undefined, undefined, cache), { kind: "none" });
  assert.equal((await choose(["/c"], undefined, undefined, cache) as any).folder, "/c");
  assert.equal((await choose(["/a", "/c"], "/c", "/a", cache) as any).folder, "/c");
  assert.equal((await choose(["/a", "/c"], undefined, "/a", cache) as any).folder, "/a");
  assert.equal((await choose(["/a", "/b"], undefined, undefined, cache) as any).attribution.customer, "acme");
  const ambiguous = await choose(["/a", "/c", "/d"], undefined, undefined, cache);
  assert.equal(ambiguous.kind, "choose");
  assert.match((ambiguous as any).folders[2].error.message, /No customer/);
  await assert.rejects(choose(["/d"], undefined, undefined, cache), /No customer/);
});

test("customer answers are cached briefly and failures are retried", async () => {
  let now = 0;
  let calls = 0;
  const cache = new AttributionCache(async () => {
    calls += 1;
    if (calls === 1) throw new Error("offline");
    return customer(`c${calls}`);
  }, () => now);
  await assert.rejects(cache.get("/a"), /offline/);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal((await cache.get("/a")).customer, "c2");
  now += 29999;
  assert.equal((await cache.get("/a")).customer, "c2");
  now += 2;
  assert.equal((await cache.get("/a")).customer, "c3");
});
