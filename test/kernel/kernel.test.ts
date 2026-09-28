import { describe, expect, it } from "vitest";
import { conditions, makeHarness, type Harness } from "../helpers/harness.ts";
import { BODIES, expectedStrongETag } from "../fixtures.ts";
import {
  logAssertion,
  logObservation,
  logOutcome,
  logRequest,
  logSteps,
  scenarioHeader,
} from "../helpers/testLog.ts";

function put(h: Harness, client: string, id: string, body: string, extra: Record<string, string> = {}) {
  const corr = h.nextCorrelation(client);
  const headerConds = conditions({
    ...(extra["if-match"] ? { ifMatch: extra["if-match"] } : {}),
    ...(extra["if-none-match"] ? { ifNoneMatch: extra["if-none-match"] } : {}),
    ...(extra["idempotency-key"] ? { idempotencyKey: extra["idempotency-key"] } : {}),
  });
  logRequest({ ...corr }, { method: "PUT", resourceId: id, bodyPreview: body, headers: extra });
  const outcome = h.kernel.put({ resourceId: id, conditions: headerConds, correlation: corr, body, contentType: "text/plain" });
  logOutcome({ ...corr }, {
    status: outcome.status,
    verdict: outcome.record.verdict,
    category: outcome.record.failureCategory,
    resultingVersion: outcome.record.resultingVersion,
    resultingEtag: outcome.record.resultingEtag,
  });
  logSteps({ ...corr }, outcome.record.steps);
  return outcome;
}

function get(h: Harness, client: string, id: string, extra: Record<string, string> = {}) {
  const corr = h.nextCorrelation(client);
  const cond = conditions({
    ...(extra["if-match"] ? { ifMatch: extra["if-match"] } : {}),
    ...(extra["if-none-match"] ? { ifNoneMatch: extra["if-none-match"] } : {}),
    ...(extra["if-modified-since"] ? { ifModifiedSince: extra["if-modified-since"] } : {}),
    ...(extra["if-unmodified-since"] ? { ifUnmodifiedSince: extra["if-unmodified-since"] } : {}),
  });
  logRequest({ ...corr }, { method: "GET", resourceId: id, headers: extra });
  const outcome = h.kernel.get({ resourceId: id, conditions: cond, correlation: corr });
  logOutcome({ ...corr }, {
    status: outcome.status,
    verdict: outcome.record.verdict,
    category: outcome.record.failureCategory,
  });
  return outcome;
}

describe("kernel resource lifecycle", () => {
  it("creates with 201 then updates with 200, gap-free versions and body-derived ETags", () => {
    const h = makeHarness({ runId: "lifecycle" });
    scenarioHeader("lifecycle", "create/update versions");

    const created = put(h, "writer", "docs/1", BODIES.alpha.body);
    expect(created.kind).toBe("present");
    expect(created.status).toBe(201);
    if (created.kind !== "present") throw new Error("narrow");
    expect(created.snapshot.version).toBe(1);
    expect(created.snapshot.canonicalEtag).toBe(expectedStrongETag(1, BODIES.alpha));
    expect(created.snapshot.body).toBe("alpha");
    logAssertion("lifecycle", created.snapshot.etagHeader === expectedStrongETag(1, BODIES.alpha), "strong emission has no W/");

    const updated = put(h, "writer", "docs/1", BODIES.beta.body);
    if (updated.kind !== "present") throw new Error("narrow");
    expect(updated.status).toBe(200);
    expect(updated.snapshot.version).toBe(2);
    expect(updated.snapshot.canonicalEtag).toBe(expectedStrongETag(2, BODIES.beta));

    const fetched = get(h, "reader", "docs/1");
    if (fetched.kind !== "present") throw new Error("narrow");
    expect(fetched.snapshot.version).toBe(2);
    expect(fetched.snapshot.body).toBe("beta");
    // Response body and ETag come from the same committed snapshot.
    expect(fetched.snapshot.canonicalEtag).toBe(expectedStrongETag(2, BODIES.beta));

    const history = h.store.listHistory("docs/1");
    expect(history.map((x) => x.version)).toEqual([1, 2]);
    expect(history[0]!.etag).toBe(expectedStrongETag(1, BODIES.alpha));
    h.close();
  });

  it("DELETE removes the resource (204) and a later GET is 404, distinct from 412", () => {
    const h = makeHarness({ runId: "delete-semantics" });
    scenarioHeader("delete-semantics", "delete then 404");
    put(h, "writer", "docs/2", BODIES.alpha.body);

    const del = h.kernel.delete({
      resourceId: "docs/2",
      conditions: conditions(),
      correlation: h.nextCorrelation("writer"),
    });
    expect(del.status).toBe(204);
    expect(del.kind).toBe("no-content");

    const fetched = get(h, "reader", "docs/2");
    expect(fetched.kind).toBe("not-found");
    expect(fetched.status).toBe(404);
    expect(fetched.record.failureCategory).toBe("not-found");

    // A conditional DELETE on the same absent id is 412, not 404.
    const condDel = h.kernel.delete({
      resourceId: "docs/2",
      conditions: conditions({ ifMatch: "*" }),
      correlation: h.nextCorrelation("writer"),
    });
    expect(condDel.status).toBe(412);
    expect(condDel.kind).toBe("precondition-failed");
    if (condDel.kind === "precondition-failed") expect(condDel.category).toBe("if-match-mismatch");
    logAssertion("delete-semantics", true, "absent GET=404 not-found; absent If-Match DELETE=412 mismatch");
    h.close();
  });

  it("re-creating after DELETE continues versions from history (no version reuse)", () => {
    const h = makeHarness({ runId: "recreate" });
    put(h, "writer", "docs/3", BODIES.alpha.body);
    h.kernel.delete({ resourceId: "docs/3", conditions: conditions(), correlation: h.nextCorrelation("writer") });
    const recreated = put(h, "writer", "docs/3", BODIES.gamma.body);
    if (recreated.kind !== "present") throw new Error("narrow");
    expect(recreated.status).toBe(201);
    expect(recreated.snapshot.version).toBe(2);
    expect(recreated.snapshot.canonicalEtag).toBe(expectedStrongETag(2, BODIES.gamma));
    h.close();
  });
});

describe("kernel conditional updates", () => {
  it("PUT with stale If-Match is rejected 412 and leaves state untouched (no lost update)", () => {
    const h = makeHarness({ runId: "stale-im" });
    scenarioHeader("stale-im", "stale If-Match rejected");
    const first = put(h, "writer-A", "docs/4", BODIES.alpha.body);
    if (first.kind !== "present") throw new Error("narrow");
    // Pretend a client held an even older validator.
    const stale = put(h, "writer-B", "docs/4", BODIES.beta.body, { "if-match": '"v0-deadbeef0000"' });
    expect(stale.kind).toBe("precondition-failed");
    expect(stale.status).toBe(412);
    if (stale.kind === "precondition-failed") expect(stale.category).toBe("if-match-mismatch");

    const current = get(h, "reader", "docs/4");
    if (current.kind !== "present") throw new Error("narrow");
    expect(current.snapshot.version).toBe(1);
    expect(current.snapshot.body).toBe("alpha");
    logAssertion("stale-im", true, "state unchanged after failed conditional PUT");
    h.close();
  });

  it("PUT with current If-Match succeeds and bumps the version", () => {
    const h = makeHarness({ runId: "current-im" });
    put(h, "writer-A", "docs/5", BODIES.alpha.body);
    const fetched = get(h, "reader", "docs/5");
    if (fetched.kind !== "present") throw new Error("narrow");
    const ok2 = put(h, "writer-B", "docs/5", BODIES.delta.body, { "if-match": fetched.snapshot.canonicalEtag });
    if (ok2.kind !== "present") throw new Error("narrow");
    expect(ok2.snapshot.version).toBe(2);
    expect(ok2.snapshot.body).toBe("delta");
    h.close();
  });

  it("weak If-Match on PUT is rejected even when opaque tag matches", () => {
    const h = makeHarness({ runId: "weak-im-write" });
    put(h, "writer", "docs/6", BODIES.alpha.body);
    const rejected = put(h, "writer", "docs/6", BODIES.beta.body, {
      "if-match": `W/${expectedStrongETag(1, BODIES.alpha)}`,
    });
    expect(rejected.status).toBe(412);
    h.close();
  });

  it("If-None-Match * blocks overwriting an existing resource (412)", () => {
    const h = makeHarness({ runId: "inm-star" });
    put(h, "writer", "docs/7", BODIES.alpha.body);
    const blocked = put(h, "writer", "docs/7", BODIES.beta.body, { "if-none-match": "*" });
    expect(blocked.status).toBe(412);
    if (blocked.kind === "precondition-failed") expect(blocked.category).toBe("if-none-match-exists");
    // But * allows creation when absent.
    const created = put(h, "writer", "docs/7b", BODIES.beta.body, { "if-none-match": "*" });
    expect(created.status).toBe(201);
    h.close();
  });

  it("GET with current If-None-Match yields 304 with ETag but no body semantics", () => {
    const h = makeHarness({ runId: "inm-get" });
    put(h, "writer", "docs/8", BODIES.alpha.body);
    const fetched = get(h, "reader", "docs/8");
    if (fetched.kind !== "present") throw new Error("narrow");
    const notMod = get(h, "reader", "docs/8", { "if-none-match": fetched.snapshot.canonicalEtag });
    expect(notMod.kind).toBe("not-modified");
    expect(notMod.status).toBe(304);
    if (notMod.kind === "not-modified") {
      expect(notMod.snapshot.canonicalEtag).toBe(fetched.snapshot.canonicalEtag);
    }
    h.close();
  });

  it("malformed headers return 400 with a specific failure category", () => {
    const h = makeHarness({ runId: "malformed" });
    put(h, "writer", "docs/9", BODIES.alpha.body);
    const badIM = put(h, "writer", "docs/9", BODIES.beta.body, { "if-match": "junk" });
    expect(badIM.status).toBe(400);
    if (badIM.kind === "malformed") expect(badIM.category).toBe("malformed-if-match");

    const badDate = get(h, "reader", "docs/9", { "if-unmodified-since": "not-a-date" });
    expect(badDate.status).toBe(400);
    if (badDate.kind === "malformed") expect(badDate.category).toBe("malformed-date");
    h.close();
  });
});

describe("weak emission mode", () => {
  it("stores canonical strong tags but emits W/ validators on the wire", () => {
    const h = makeHarness({ runId: "weak-emit", etagStrength: "weak" });
    const created = put(h, "writer", "docs/w", BODIES.alpha.body);
    if (created.kind !== "present") throw new Error("narrow");
    expect(created.snapshot.etagHeader.startsWith("W/")).toBe(true);
    expect(created.snapshot.canonicalEtag).toBe(expectedStrongETag(1, BODIES.alpha));

    // Weakly-returned validator can be echoed back on a GET (weak compare).
    const fetched = get(h, "reader", "docs/w");
    if (fetched.kind !== "present") throw new Error("narrow");
    const notMod = get(h, "reader", "docs/w", { "if-none-match": fetched.snapshot.etagHeader });
    expect(notMod.status).toBe(304);
    h.close();
  });
});

describe("idempotent replay after a lost response", () => {
  it("replays the original outcome with the same version/ETag and marks replayed=true", () => {
    const h = makeHarness({ runId: "lost-replay" });
    scenarioHeader("lost-replay", "same key + same body replays verbatim");
    const key = "postillon-2026-09-27-0001";
    const first = put(h, "writer", "lost/doc", BODIES.firstDraft.body, { "idempotency-key": key });
    if (first.kind !== "present") throw new Error("narrow");
    expect(first.status).toBe(201);
    expect(first.replayed).toBe(false);
    logObservation(
      { runId: h.runId, clientId: "writer", requestId: "orig" },
      { version: first.snapshot.version, etag: first.snapshot.canonicalEtag, updatedAtMs: first.snapshot.updatedAtMs },
    );

    // Response was lost: client retries the exact same request.
    const retry = put(h, "writer", "lost/doc", BODIES.firstDraft.body, { "idempotency-key": key });
    if (retry.kind !== "present") throw new Error("narrow");
    expect(retry.replayed).toBe(true);
    expect(retry.status).toBe(201);
    expect(retry.snapshot.version).toBe(1);
    expect(retry.snapshot.body).toBe(BODIES.firstDraft.body);
    expect(retry.snapshot.canonicalEtag).toBe(first.snapshot.canonicalEtag);
    logAssertion("lost-replay", retry.snapshot.version === 1, "replay did NOT create a duplicate version");

    // The decisions log shows both attempts, the second flagged replayed.
    const decisions = h.store.listDecisions({ runId: "lost-replay" });
    const puts = decisions.filter((d) => d.method === "PUT");
    expect(puts).toHaveLength(2);
    expect(puts[0]!.replayed).toBe(false);
    expect(puts[1]!.replayed).toBe(true);
    h.close();
  });

  it("reusing a key with a different body fails 409 idempotency-replay-conflict", () => {
    const h = makeHarness({ runId: "replay-conflict" });
    const key = "postillon-2026-09-27-0002";
    put(h, "writer", "lost/doc2", BODIES.alpha.body, { "idempotency-key": key });
    const conflict = put(h, "writer", "lost/doc2", BODIES.beta.body, { "idempotency-key": key });
    expect(conflict.kind).toBe("replay-conflict");
    expect(conflict.status).toBe(409);
    if (conflict.kind === "replay-conflict") {
      expect(conflict.category).toBe("idempotency-replay-conflict");
    }
    // Nothing was written by the conflicting attempt.
    const current = get(h, "reader", "lost/doc2");
    if (current.kind !== "present") throw new Error("narrow");
    expect(current.snapshot.body).toBe("alpha");
    h.close();
  });

  it("replays DELETE as 204 without deleting twice", () => {
    const h = makeHarness({ runId: "delete-replay" });
    put(h, "writer", "lost/doc3", BODIES.alpha.body);
    const key = "del-0001";
    const d1 = h.kernel.delete({
      resourceId: "lost/doc3",
      conditions: conditions({ idempotencyKey: key }),
      correlation: h.nextCorrelation("writer"),
    });
    expect(d1.status).toBe(204);
    const d2 = h.kernel.delete({
      resourceId: "lost/doc3",
      conditions: conditions({ idempotencyKey: key }),
      correlation: h.nextCorrelation("writer"),
    });
    expect(d2.status).toBe(204);
    expect(d2.kind).toBe("no-content");
    if (d2.kind === "no-content") expect(d2.replayed).toBe(true);
    h.close();
  });

  it("reusing a key for a DIFFERENT resource or method is a 409 scope conflict (no cross-resource replay)", () => {
    const h = makeHarness({ runId: "replay-scope" });
    scenarioHeader("replay-scope", "idempotency key is bound to method + resource + body");
    const key = "scoped-key-1";

    const first = put(h, "writer", "scope/a", BODIES.alpha.body, { "idempotency-key": key });
    expect(first.status).toBe(201);

    // Same key + same body, different resource -> must NOT return a's snapshot.
    const otherResource = put(h, "writer", "scope/b", BODIES.alpha.body, { "idempotency-key": key });
    expect(otherResource.kind).toBe("replay-conflict");
    expect(otherResource.status).toBe(409);
    if (otherResource.kind === "replay-conflict") {
      expect(otherResource.category).toBe("idempotency-replay-conflict");
    }
    const b = get(h, "reader", "scope/b");
    expect(b.kind).toBe("not-found"); // nothing written for scope/b

    // Same key + same resource, different method (DELETE) -> also 409.
    const otherMethod = h.kernel.delete({
      resourceId: "scope/a",
      conditions: conditions({ idempotencyKey: key }),
      correlation: h.nextCorrelation("writer"),
    });
    expect(otherMethod.status).toBe(409);
    expect(otherMethod.kind).toBe("replay-conflict");
    // Original resource untouched by the conflicting DELETE.
    const stillThere = get(h, "reader", "scope/a");
    expect(stillThere.kind).toBe("present");
    if (stillThere.kind === "present") expect(stillThere.snapshot.body).toBe("alpha");
    h.close();
  });
});
