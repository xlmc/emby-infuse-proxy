import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const workerSource = await readFile(new URL("../worker.js", import.meta.url), "utf8");
const instrumentedSource = workerSource.replace(
  /var \{ workerHandler: Nh \} = Dh\(\);\s*export \{\s*Nh as default\s*\};\s*$/,
  "export { kg, Jg, buildItemsLatestMicrocacheKey, ne };"
);
assert.notEqual(instrumentedSource, workerSource, "worker test export injection failed");
const worker = await import(`data:text/javascript;base64,${Buffer.from(instrumentedSource).toString("base64")}`);

class FakeCache {
  entries = new Map();

  async match(request) {
    const key = request instanceof Request ? request.url : String(request);
    const entry = this.entries.get(key);
    if (!entry || entry.expiresAt <= Date.now()) {
      this.entries.delete(key);
      return undefined;
    }
    return entry.response.clone();
  }

  async put(request, response) {
    const key = request instanceof Request ? request.url : String(request);
    const maxAge = Number(/max-age=(\d+)/i.exec(response.headers.get("Cache-Control") || "")?.[1] || 0);
    const body = await response.arrayBuffer();
    this.entries.set(key, {
      expiresAt: Date.now() + maxAge * 1000,
      response: new Response(body, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers
      })
    });
  }
}

function wireService(logs) {
  const dependencies = {
    buildProxyResponseHeaders(response) {
      return new Headers(response.headers);
    },
    recordAccessLog(_context, payload) {
      logs.push(payload);
    }
  };
  const service = worker.kg({}, dependencies);
  dependencies.tryServeItemsLatestCache = service.tryServeItemsLatestCache;
  dependencies.storeItemsLatestCache = service.storeItemsLatestCache;
  return service;
}

async function makeKey(identity) {
  return worker.buildItemsLatestMicrocacheKey({
    origin: "https://example.test",
    nodeName: "emby",
    nodeRevision: "revision-1",
    proxyPath: "/Users/user-1/Items/Latest",
    search: "?Limit=30&Fields=PrimaryImageAspectRatio",
    identity,
    corsOrigin: "*"
  });
}

function makeContext(cache, key) {
  return {
    requestTraits: { isItemsLatestRequest: true },
    itemsLatestCache: cache,
    itemsLatestCacheKey: key,
    request: new Request("https://example.test/Users/user-1/Items/Latest?Limit=30"),
    dynamicCors: {},
    finalOrigin: "*",
    startTime: Date.now(),
    nodeName: "emby",
    proxyPath: "/Users/user-1/Items/Latest",
    requestMethod: "GET",
    clientIp: "test",
    currentConfig: {},
    env: {},
    ctx: { waitUntil() {} }
  };
}

test("coalesces seven identical Items/Latest requests and expires after five seconds", async () => {
  const logs = [];
  const service = wireService(logs);
  const cache = new FakeCache();
  const context = makeContext(cache, await makeKey("account-a"));
  let upstreamCalls = 0;
  const fetchLatest = async () => {
    upstreamCalls += 1;
    await new Promise((resolve) => setTimeout(resolve, 40));
    return new Response(JSON.stringify([{ Id: "vl-1", Name: "Example" }]), {
      headers: { "Content-Type": "application/json; charset=utf-8" }
    });
  };

  const responses = await Promise.all(
    Array.from({ length: 7 }, () => service.runItemsLatestSingleFlight({ ...context }, fetchLatest))
  );
  assert.equal(upstreamCalls, 1);
  assert.deepEqual(await Promise.all(responses.map((response) => response.json())), Array(7).fill([{ Id: "vl-1", Name: "Example" }]));
  assert.equal(responses.filter((response) => response.headers.get("X-Infuse-Latest-Cache") === "singleflight_join").length, 6);

  const immediate = await service.runItemsLatestSingleFlight({ ...context }, fetchLatest);
  assert.equal(immediate.headers.get("X-Infuse-Latest-Cache"), "race_hit");
  assert.equal(upstreamCalls, 1);

  await new Promise((resolve) => setTimeout(resolve, 5100));
  await service.runItemsLatestSingleFlight({ ...context }, fetchLatest);
  assert.equal(upstreamCalls, 2);
  assert.equal(logs.filter((entry) => /ItemsLatestCache=/.test(entry.errorDetail || "")).length >= 7, true);
});

test("partitions cache keys by authenticated identity", async () => {
  const accountA = await makeKey("account-a");
  const accountB = await makeKey("account-b");
  assert.notEqual(accountA.url, accountB.url);
  assert.equal(new URL(accountA.url).origin, "https://example.test");
  assert.doesNotMatch(accountA.url, /account-a|user-1/i);
  assert.doesNotMatch(accountB.url, /account-b|user-1/i);
});

test("only classifies exact GET Items/Latest routes for microcaching", () => {
  const classify = worker.Jg({}, {}).classifyRequest;
  const config = {};
  const options = {};
  const traits = (method, path) => classify(
    new Request(`https://example.test${path}`, { method }),
    path,
    new URL(`https://example.test${path}`),
    config,
    options
  );

  assert.equal(traits("GET", "/Users/user-1/Items/Latest").isItemsLatestRequest, true);
  assert.equal(traits("GET", "/emby/Users/user-1/Items/Latest").isItemsLatestRequest, true);
  assert.equal(traits("POST", "/Users/user-1/Items/Latest").isItemsLatestRequest, false);
  assert.equal(traits("GET", "/Users/user-1/Items/Resume").isItemsLatestRequest, false);
  assert.equal(traits("GET", "/Items/vl-1/PlaybackInfo").isItemsLatestRequest, false);
  assert.equal(traits("GET", "/Videos/vl-1/stream").isItemsLatestRequest, false);
});
