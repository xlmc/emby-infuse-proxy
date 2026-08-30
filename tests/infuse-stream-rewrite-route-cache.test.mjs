import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const workerSource = await readFile(new URL("../worker.js", import.meta.url), "utf8");
const instrumentedSource = workerSource.replace(
  /var \{ workerHandler: Nh \} = Dh\(\);\s*export \{\s*Nh as default\s*\};\s*$/,
  "export { Ol, indexInfuseStreamRewriteRoutes, ne };"
);
assert.notEqual(instrumentedSource, workerSource, "worker test export injection failed");
const worker = await import(`data:text/javascript;base64,${Buffer.from(instrumentedSource).toString("base64")}`);

function playbackContext(token, mediaSourceId = "source-1") {
  return {
    nodeName: "emos",
    nodeDerivedCacheRevision: "revision-a",
    proxyPath: "/Items/ve-23421/PlaybackInfo",
    requestUrl: new URL(`https://proxy.test/Items/ve-23421/PlaybackInfo?api_key=${token}`),
    request: new Request(`https://proxy.test/Items/ve-23421/PlaybackInfo?api_key=${token}`, {
      headers: { "User-Agent": "Infuse-Direct/8.5.3" }
    }),
    mediaSourceId
  };
}

function streamContext(token, mediaSourceId = "source-1") {
  return {
    nodeName: "emos",
    nodeDerivedCacheRevision: "revision-a",
    proxyPath: "/Videos/ve-23421/stream",
    requestUrl: new URL(`https://proxy.test/Videos/ve-23421/stream?MediaSourceId=${mediaSourceId}&api_key=${token}`),
    request: new Request(`https://proxy.test/Videos/ve-23421/stream?MediaSourceId=${mediaSourceId}&api_key=${token}`, {
      headers: { "User-Agent": "Infuse-Direct/8.5.3" }
    })
  };
}

test("standard Infuse PlaybackInfo records a negative route and skips the duplicate lookup", async () => {
  worker.ne.InfuseStreamRewriteRouteCache.clear();
  const context = playbackContext("account-a");
  await worker.indexInfuseStreamRewriteRoutes(context, {
    MediaSources: [{ Id: "source-1", DirectStreamUrl: "/Videos/ve-23421/stream?MediaSourceId=source-1" }]
  }, new URL("https://origin.test"));

  let upstreamCalls = 0;
  const stream = streamContext("account-a");
  const route = await worker.Ol(stream, new URL("https://origin.test"), {
    fetch: async () => {
      upstreamCalls += 1;
      return new Response("unexpected", { status: 500 });
    }
  });
  assert.equal(route, null);
  assert.equal(upstreamCalls, 0);
  assert.equal(stream.infuseStreamRewriteCacheState, "hit_standard");
});

test("the same token matches when PlaybackInfo uses a header and stream uses a query parameter", async () => {
  worker.ne.InfuseStreamRewriteRouteCache.clear();
  const context = playbackContext("placeholder");
  context.requestUrl = new URL("https://proxy.test/Items/ve-23421/PlaybackInfo");
  context.request = new Request(context.requestUrl, {
    headers: {
      "User-Agent": "Infuse-Direct/8.5.3",
      "X-Emby-Token": "account-a"
    }
  });
  await worker.indexInfuseStreamRewriteRoutes(context, {
    MediaSources: [{ Id: "source-1", DirectStreamUrl: "/Videos/ve-23421/stream?MediaSourceId=source-1" }]
  }, new URL("https://origin.test"));

  let upstreamCalls = 0;
  const stream = streamContext("account-a");
  const route = await worker.Ol(stream, new URL("https://origin.test"), {
    fetch: async () => {
      upstreamCalls += 1;
      return new Response("unexpected", { status: 500 });
    }
  });
  assert.equal(route, null);
  assert.equal(upstreamCalls, 0);
  assert.equal(stream.infuseStreamRewriteCacheState, "hit_standard");
});

test("edge cache survives an empty Worker isolate memory cache", async () => {
  const stored = new Map();
  const previousCaches = globalThis.caches;
  globalThis.caches = {
    default: {
      async put(request, response) {
        stored.set(request.url, response.clone());
      },
      async match(request) {
        return stored.get(request.url)?.clone();
      }
    }
  };
  try {
    worker.ne.InfuseStreamRewriteRouteCache.clear();
    await worker.indexInfuseStreamRewriteRoutes(playbackContext("account-a"), {
      MediaSources: [{ Id: "source-1", DirectStreamUrl: "/Videos/ve-23421/stream?MediaSourceId=source-1" }]
    }, new URL("https://origin.test"));
    assert.ok(stored.size > 0);
    for (const response of stored.values()) {
      assert.equal(response.headers.get("Cache-Control"), "public, max-age=1800");
    }

    worker.ne.InfuseStreamRewriteRouteCache.clear();
    let upstreamCalls = 0;
    const stream = streamContext("account-a");
    const route = await worker.Ol(stream, new URL("https://origin.test"), {
      fetch: async () => {
        upstreamCalls += 1;
        return new Response("unexpected", { status: 500 });
      }
    });
    assert.equal(route, null);
    assert.equal(upstreamCalls, 0);
    assert.equal(stream.infuseStreamRewriteCacheState, "hit_edge_standard");
  } finally {
    if (previousCaches === undefined) delete globalThis.caches;
    else globalThis.caches = previousCaches;
  }
});

test("emya route keeps its rewritten path and query without another upstream lookup", async () => {
  worker.ne.InfuseStreamRewriteRouteCache.clear();
  const context = playbackContext("account-a");
  await worker.indexInfuseStreamRewriteRoutes(context, {
    MediaSources: [{ Id: "source-1", DirectStreamUrl: "/emya/video?MediaSourceId=source-1&api_key=account-a" }]
  }, new URL("https://origin.test"));

  let upstreamCalls = 0;
  const stream = streamContext("account-a");
  const route = await worker.Ol(stream, new URL("https://origin.test"), {
    fetch: async () => {
      upstreamCalls += 1;
      return new Response("unexpected", { status: 500 });
    }
  });
  assert.deepEqual(route, {
    proxyPath: "/emya/video",
    search: "?MediaSourceId=source-1&api_key=account-a",
    upstreamPath: "/emya/video"
  });
  assert.equal(upstreamCalls, 0);
  assert.equal(stream.infuseStreamRewriteCacheState, "hit_emya");
});

test("multiple standard media sources use an authenticated item fallback", async () => {
  worker.ne.InfuseStreamRewriteRouteCache.clear();
  await worker.indexInfuseStreamRewriteRoutes(playbackContext("account-a"), {
    MediaSources: [
      { Id: "source-1", DirectStreamUrl: "/Videos/ve-23421/stream?MediaSourceId=source-1" },
      { Id: "source-2", DirectStreamUrl: "/Videos/ve-23421/stream?MediaSourceId=source-2" }
    ]
  }, new URL("https://origin.test"));

  let upstreamCalls = 0;
  const stream = streamContext("account-a", "client-selected-source");
  const route = await worker.Ol(stream, new URL("https://origin.test"), {
    fetch: async () => {
      upstreamCalls += 1;
      return new Response("unexpected", { status: 500 });
    }
  });
  assert.equal(route, null);
  assert.equal(upstreamCalls, 0);
  assert.equal(stream.infuseStreamRewriteCacheState, "hit_standard");
});

test("route cache is partitioned by authentication", async () => {
  worker.ne.InfuseStreamRewriteRouteCache.clear();
  await worker.indexInfuseStreamRewriteRoutes(playbackContext("account-a"), {
    MediaSources: [{ Id: "source-1", DirectStreamUrl: "/Videos/ve-23421/stream?MediaSourceId=source-1" }]
  }, new URL("https://origin.test"));

  let upstreamCalls = 0;
  const stream = streamContext("account-b");
  const route = await worker.Ol(stream, new URL("https://origin.test"), {
    fetch: async () => {
      upstreamCalls += 1;
      return Response.json({ MediaSources: [] });
    }
  });
  assert.equal(route, null);
  assert.equal(upstreamCalls, 1);
  assert.equal(stream.infuseStreamRewriteCacheState, "miss_auth_fetch");
});

test("route cache is partitioned by node revision", async () => {
  worker.ne.InfuseStreamRewriteRouteCache.clear();
  await worker.indexInfuseStreamRewriteRoutes(playbackContext("account-a"), {
    MediaSources: [{ Id: "source-1", DirectStreamUrl: "/Videos/ve-23421/stream?MediaSourceId=source-1" }]
  }, new URL("https://origin.test"));

  let upstreamCalls = 0;
  const stream = streamContext("account-a");
  stream.nodeDerivedCacheRevision = "revision-b";
  const route = await worker.Ol(stream, new URL("https://origin.test"), {
    fetch: async () => {
      upstreamCalls += 1;
      return Response.json({ MediaSources: [] });
    }
  });
  assert.equal(route, null);
  assert.equal(upstreamCalls, 1);
  assert.equal(stream.infuseStreamRewriteCacheState, "miss_auth_fetch");
});
