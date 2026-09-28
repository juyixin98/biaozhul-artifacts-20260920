import { afterEach, describe, expect, it } from "vitest";
import { buildApp } from "../../src/http/app.js";
import type { FastifyInstance } from "fastify";
import { Kernel } from "../../src/kernel/kernel.js";
import { SqliteResourceStore } from "../../src/state/sqliteStore.js";
import type { ResourceStore } from "../../src/state/store.js";
import { BODIES, expectedStrongETag } from "../fixtures.ts";

interface TestServer {
  app: FastifyInstance;
  store: ResourceStore;
  close(): Promise<void>;
}

async function makeServer(strength: "strong" | "weak" = "strong"): Promise<TestServer> {
  const store = new SqliteResourceStore({ path: ":memory:", busyTimeoutMs: 2000 });
  const kernel = new Kernel({ store, etagStrength: strength, clock: () => Date.now() });
  const app = await buildApp({ kernel, store, etagStrength: strength, diagnostics: false });
  return {
    app,
    store,
    async close() {
      await app.close();
      store.close();
    },
  };
}

const servers: TestServer[] = [];
afterEach(async () => {
  while (servers.length) {
    const s = servers.pop()!;
    await s.close();
  }
});

describe("HTTP resource API", () => {
  it("PUT creates (201) with ETag/Last-Modified/version headers", async () => {
    const s = await makeServer();
    servers.push(s);
    const res = await s.app.inject({
      method: "PUT",
      url: "/resources/docs/http-1",
      headers: { "content-type": "text/plain", "x-run-id": "http-create", "x-client-id": "writer" },
      payload: BODIES.alpha.body,
    });
    expect(res.statusCode).toBe(201);
    expect(res.headers["etag"]).toBe(expectedStrongETag(1, BODIES.alpha));
    expect(res.headers["x-resource-version"]).toBe("1");
    expect(res.headers["last-modified"]).toMatch(/GMT$/);
    expect(res.body).toBe("alpha");
  });

  it("GET returns body and ETag from the same committed snapshot", async () => {
    const s = await makeServer();
    servers.push(s);
    await s.app.inject({
      method: "PUT",
      url: "/resources/docs/http-2",
      headers: { "content-type": "text/plain" },
      payload: BODIES.beta.body,
    });
    const res = await s.app.inject({ method: "GET", url: "/resources/docs/http-2" });
    expect(res.statusCode).toBe(200);
    expect(res.body).toBe("beta");
    expect(res.headers["etag"]).toBe(expectedStrongETag(1, BODIES.beta));
    expect(res.headers["x-resource-version"]).toBe("1");
  });

  it("If-None-Match current -> 304 with ETag header and empty body", async () => {
    const s = await makeServer();
    servers.push(s);
    const put = await s.app.inject({
      method: "PUT",
      url: "/resources/docs/http-3",
      headers: { "content-type": "text/plain" },
      payload: BODIES.gamma.body,
    });
    const etag = put.headers["etag"] as string;
    const res = await s.app.inject({
      method: "GET",
      url: "/resources/docs/http-3",
      headers: { "if-none-match": etag },
    });
    expect(res.statusCode).toBe(304);
    expect(res.body).toBe("");
    expect(res.headers["etag"]).toBe(etag);
  });

  it("weak-tag mode emits W/ validators and weak echo still validates on GET", async () => {
    const s = await makeServer("weak");
    servers.push(s);
    const put = await s.app.inject({
      method: "PUT",
      url: "/resources/docs/http-weak",
      headers: { "content-type": "text/plain" },
      payload: BODIES.alpha.body,
    });
    expect(put.headers["etag"]).toBe(`W/${expectedStrongETag(1, BODIES.alpha)}`);

    // A weak validator echoed on a PUT's If-Match must be rejected (strong compare).
    const weakPut = await s.app.inject({
      method: "PUT",
      url: "/resources/docs/http-weak",
      headers: { "content-type": "text/plain", "if-match": put.headers["etag"] as string },
      payload: BODIES.beta.body,
    });
    expect(weakPut.statusCode).toBe(412);
    expect(weakPut.json().error.category).toBe("if-match-mismatch");

    // But weak comparison on GET If-None-Match matches.
    const weakGet = await s.app.inject({
      method: "GET",
      url: "/resources/docs/http-weak",
      headers: { "if-none-match": put.headers["etag"] as string },
    });
    expect(weakGet.statusCode).toBe(304);
  });

  it("404 (absent) and 412 If-Match mismatch are distinct responses", async () => {
    const s = await makeServer();
    servers.push(s);

    const plain = await s.app.inject({ method: "GET", url: "/resources/docs/missing" });
    expect(plain.statusCode).toBe(404);
    expect(plain.json().error.category).toBe("not-found");

    const conditional = await s.app.inject({
      method: "PUT",
      url: "/resources/docs/missing",
      headers: { "content-type": "text/plain", "if-match": '"v9-zzzzzzzzzzzz"' },
      payload: BODIES.alpha.body,
    });
    expect(conditional.statusCode).toBe(412);
    expect(conditional.json().error.category).toBe("if-match-mismatch");
  });

  it("wildcard: If-None-Match * allows create on absent id, blocks overwrite", async () => {
    const s = await makeServer();
    servers.push(s);
    const create = await s.app.inject({
      method: "PUT",
      url: "/resources/docs/wild",
      headers: { "content-type": "text/plain", "if-none-match": "*" },
      payload: BODIES.alpha.body,
    });
    expect(create.statusCode).toBe(201);

    const overwrite = await s.app.inject({
      method: "PUT",
      url: "/resources/docs/wild",
      headers: { "content-type": "text/plain", "if-none-match": "*" },
      payload: BODIES.beta.body,
    });
    expect(overwrite.statusCode).toBe(412);
    expect(overwrite.json().error.category).toBe("if-none-match-exists");
  });

  it("If-Match * requires existence on update", async () => {
    const s = await makeServer();
    servers.push(s);
    const res = await s.app.inject({
      method: "PUT",
      url: "/resources/docs/never-existed",
      headers: { "content-type": "text/plain", "if-match": "*" },
      payload: BODIES.alpha.body,
    });
    expect(res.statusCode).toBe(412);
    expect(res.json().error.category).toBe("if-match-mismatch");
  });

  it("malformed conditional headers return 400 with a specific category, never a false success", async () => {
    const s = await makeServer();
    servers.push(s);
    const badTag = await s.app.inject({
      method: "GET",
      url: "/resources/docs/anything",
      headers: { "if-none-match": "not-a-valid-etag" },
    });
    expect(badTag.statusCode).toBe(400);
    expect(badTag.json().error.category).toBe("malformed-if-none-match");

    const badDate = await s.app.inject({
      method: "GET",
      url: "/resources/docs/anything",
      headers: { "if-unmodified-since": "definitely-not-a-date" },
    });
    expect(badDate.statusCode).toBe(400);
    expect(badDate.json().error.category).toBe("malformed-date");
  });

  it("idempotent retry after a lost response returns the original snapshot and Idempotent-Replay: true", async () => {
    const s = await makeServer();
    servers.push(s);
    const headers = {
      "content-type": "text/plain",
      "idempotency-key": "replay-key-http-1",
      "x-run-id": "http-replay",
      "x-client-id": "writer",
    };
    const first = await s.app.inject({
      method: "PUT",
      url: "/resources/docs/replayable",
      headers,
      payload: BODIES.firstDraft.body,
    });
    expect(first.statusCode).toBe(201);
    expect(first.headers["idempotent-replay"]).toBeUndefined();

    const retry = await s.app.inject({
      method: "PUT",
      url: "/resources/docs/replayable",
      headers,
      payload: BODIES.firstDraft.body,
    });
    expect(retry.statusCode).toBe(201);
    expect(retry.headers["idempotent-replay"]).toBe("true");
    expect(retry.headers["etag"]).toBe(first.headers["etag"]);
    expect(retry.headers["x-resource-version"]).toBe("1");
    expect(retry.body).toBe(BODIES.firstDraft.body);

    // Reusing the key with a different body is a 409 conflict.
    const conflict = await s.app.inject({
      method: "PUT",
      url: "/resources/docs/replayable",
      headers: { ...headers, "idempotency-key": "replay-key-http-1" },
      payload: BODIES.beta.body,
    });
    expect(conflict.statusCode).toBe(409);
    expect(conflict.json().error.category).toBe("idempotency-replay-conflict");
  });

  it("diagnostics endpoint correlates run/client and exposes versions, steps and verdicts", async () => {
    const s = await makeServer();
    servers.push(s);
    await s.app.inject({
      method: "PUT",
      url: "/resources/docs/diag",
      headers: { "content-type": "text/plain", "x-run-id": "run-diag", "x-client-id": "writer-A" },
      payload: BODIES.alpha.body,
    });
    const stale = await s.app.inject({
      method: "PUT",
      url: "/resources/docs/diag",
      headers: {
        "content-type": "text/plain",
        "x-run-id": "run-diag",
        "x-client-id": "writer-B",
        "if-match": '"v1-000000000000"',
      },
      payload: BODIES.beta.body,
    });
    expect(stale.statusCode).toBe(412);

    const diag = await s.app.inject({
      method: "GET",
      url: "/diagnostics/decisions?runId=run-diag",
    });
    expect(diag.statusCode).toBe(200);
    const body = diag.json();
    expect(body.count).toBe(2);
    const second = body.records[1];
    expect(second.clientId).toBe("writer-B");
    expect(second.observedVersion).toBe(1);
    expect(second.observedEtag).toBe(expectedStrongETag(1, BODIES.alpha));
    expect(second.verdict).toBe("precondition-failed");
    expect(second.failureCategory).toBe("if-match-mismatch");
    expect(second.outcomeStatus).toBe(412);
    expect(second.stepsReadable[0]).toContain("if-match");
    expect(second.stepsReadable[0]).toContain("no-match");

    const history = await s.app.inject({
      method: "GET",
      url: "/diagnostics/history?resourceId=" + encodeURIComponent("docs/diag"),
    });
    expect(history.json().versions.map((v: { version: number }) => v.version)).toEqual([1]);
  });

  it("health reports store kind", async () => {
    const s = await makeServer();
    servers.push(s);
    const res = await s.app.inject({ method: "GET", url: "/health" });
    expect(res.json()).toEqual({ status: "ok", store: "sqlite" });
  });

  it("HEAD returns headers but no body", async () => {
    const s = await makeServer();
    servers.push(s);
    await s.app.inject({
      method: "PUT",
      url: "/resources/docs/head",
      headers: { "content-type": "text/plain" },
      payload: BODIES.alpha.body,
    });
    const res = await s.app.inject({ method: "HEAD", url: "/resources/docs/head" });
    expect(res.statusCode).toBe(200);
    expect(res.body).toBe("");
    expect(res.headers["etag"]).toBe(expectedStrongETag(1, BODIES.alpha));
  });

  it("If-Modified-Since past date -> 200; future date -> 304", async () => {
    const s = await makeServer();
    servers.push(s);
    await s.app.inject({
      method: "PUT",
      url: "/resources/docs/ims",
      headers: { "content-type": "text/plain" },
      payload: BODIES.alpha.body,
    });
    const modified = await s.app.inject({
      method: "GET",
      url: "/resources/docs/ims",
      headers: { "if-modified-since": "Wed, 01 Jan 2020 00:00:00 GMT" },
    });
    expect(modified.statusCode).toBe(200);
    const cached = await s.app.inject({
      method: "GET",
      url: "/resources/docs/ims",
      headers: { "if-modified-since": "Wed, 01 Jan 2031 00:00:00 GMT" },
    });
    expect(cached.statusCode).toBe(304);
  });

  it("If-Unmodified-Since stale -> 412 with date category", async () => {
    const s = await makeServer();
    servers.push(s);
    await s.app.inject({
      method: "PUT",
      url: "/resources/docs/ius",
      headers: { "content-type": "text/plain" },
      payload: BODIES.alpha.body,
    });
    const res = await s.app.inject({
      method: "PUT",
      url: "/resources/docs/ius",
      headers: { "content-type": "text/plain", "if-unmodified-since": "Wed, 01 Jan 2020 00:00:00 GMT" },
      payload: BODIES.beta.body,
    });
    expect(res.statusCode).toBe(412);
    expect(res.json().error.category).toBe("if-unmodified-since-modified");
  });

  it("DELETE returns 204 and removes the resource", async () => {
    const s = await makeServer();
    servers.push(s);
    await s.app.inject({
      method: "PUT",
      url: "/resources/docs/del",
      headers: { "content-type": "text/plain" },
      payload: BODIES.alpha.body,
    });
    const del = await s.app.inject({ method: "DELETE", url: "/resources/docs/del" });
    expect(del.statusCode).toBe(204);
    const get = await s.app.inject({ method: "GET", url: "/resources/docs/del" });
    expect(get.statusCode).toBe(404);
  });

  it("diagnostics history requires a resourceId", async () => {
    const s = await makeServer();
    servers.push(s);
    const res = await s.app.inject({ method: "GET", url: "/diagnostics/history" });
    expect(res.statusCode).toBe(400);
  });

  it("diagnostics-enabled server logs traceable decisions for every outcome kind", async () => {
    // Drives the logOutcome/describeSteps branches (tag + date steps) that
    // only run when the DIAGNOSTICS flag is on.
    const store = new SqliteResourceStore({ path: ":memory:", busyTimeoutMs: 2000 });
    const kernel = new Kernel({ store, etagStrength: "strong", clock: () => Date.now() });
    const app = await buildApp({ kernel, store, etagStrength: "strong", diagnostics: true });
    servers.push({ app, store, close: async () => { await app.close(); store.close(); } });

    const baseHeaders = { "content-type": "text/plain", "x-run-id": "diag-on", "x-client-id": "w" };
    await app.inject({ method: "PUT", url: "/resources/d/1", headers: baseHeaders, payload: BODIES.alpha.body });
    // not-modified
    await app.inject({ method: "GET", url: "/resources/d/1", headers: { "x-run-id": "diag-on", "if-none-match": expectedStrongETag(1, BODIES.alpha) } });
    // IMS ignored because If-None-Match is present
    await app.inject({
      method: "GET",
      url: "/resources/d/1",
      headers: { "x-run-id": "diag-on", "if-none-match": '"v0-x"', "if-modified-since": "Wed, 01 Jan 2020 00:00:00 GMT" },
    });
    // malformed IMS is ignored (not an error)
    await app.inject({ method: "GET", url: "/resources/d/1", headers: { "x-run-id": "diag-on", "if-modified-since": "garbage" } });
    // IMS ignored on unsafe methods
    await app.inject({ method: "PUT", url: "/resources/d/1", headers: { ...baseHeaders, "if-modified-since": "Wed, 01 Jan 2020 00:00:00 GMT" }, payload: BODIES.alpha.body });
    // IUS unmodified (future) then modified (past)
    await app.inject({ method: "GET", url: "/resources/d/1", headers: { "x-run-id": "diag-on", "if-unmodified-since": "Wed, 01 Jan 2031 00:00:00 GMT" } });
    // precondition-failed (stale If-Match) and date-modified (stale IUS)
    await app.inject({ method: "PUT", url: "/resources/d/1", headers: { ...baseHeaders, "if-match": '"v9-x"' }, payload: BODIES.beta.body });
    await app.inject({ method: "GET", url: "/resources/d/1", headers: { "x-run-id": "diag-on", "if-unmodified-since": "Wed, 01 Jan 2020 00:00:00 GMT" } });
    // malformed conditional header
    await app.inject({ method: "GET", url: "/resources/d/1", headers: { "x-run-id": "diag-on", "if-unmodified-since": "nonsense" } });
    // date step (IMS modified -> 200)
    await app.inject({ method: "GET", url: "/resources/d/1", headers: { "x-run-id": "diag-on", "if-modified-since": "Wed, 01 Jan 2020 00:00:00 GMT" } });
    // idempotency conflict (409) logged with diagnostics on
    await app.inject({ method: "PUT", url: "/resources/d/new", headers: { ...baseHeaders, "idempotency-key": "diag-key" }, payload: BODIES.alpha.body });
    await app.inject({ method: "PUT", url: "/resources/d/new", headers: { ...baseHeaders, "idempotency-key": "diag-key" }, payload: BODIES.beta.body });
    // no-content
    await app.inject({ method: "DELETE", url: "/resources/d/1", headers: { "x-run-id": "diag-on" } });
    // not-found
    await app.inject({ method: "GET", url: "/resources/d/1", headers: { "x-run-id": "diag-on" } });

    const res = await app.inject({ method: "GET", url: "/diagnostics/decisions?runId=diag-on" });
    expect(res.statusCode).toBe(200);
    const verdicts = res.json().records.map((r: { verdict: string }) => r.verdict);
    expect(verdicts).toContain("not-modified");
    expect(verdicts).toContain("precondition-failed");
    expect(verdicts).toContain("malformed");
  });
});
