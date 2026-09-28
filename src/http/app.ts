/**
 * Transport layer — Fastify HTTP adapter.
 *
 * Maps wire headers to the contract ConditionInput, invokes the execution
 * kernel, and maps KernelOutcome to status codes / headers / bodies.
 *
 * Error responses never collapse to 200: every failure carries an explicit
 * machine-readable `category` equal to the kernel FailureCategory.
 */

import createFastify, { type FastifyInstance, type FastifyReply, type FastifyRequest } from "fastify";
import { formatHttpDate } from "../contract/httpDate.js";
import type { ConditionInput, EvalStep } from "../contract/model.js";
import type { Kernel, KernelOutcome } from "../kernel/kernel.js";
import { renderETag } from "../contract/etag.js";
import type { ResourceStore } from "../state/store.js";

export interface BuildAppDeps {
  readonly kernel: Kernel;
  readonly store: ResourceStore;
  readonly etagStrength: "strong" | "weak";
  readonly diagnostics: boolean;
}

const RESOURCE_ROUTE = "/resources/*";

/** Extract the full (slash-bearing) resource id from a wildcard route. */
function resourceIdOf(req: FastifyRequest): string {
  const wildcard = (req.params as Record<string, string | undefined>)["*"] ?? "";
  return decodeURIComponent(wildcard);
}

function extractConditions(req: FastifyRequest): ConditionInput {
  const h = req.headers;
  return {
    ifMatch: optionalHeader(h["if-match"]),
    ifNoneMatch: optionalHeader(h["if-none-match"]),
    ifUnmodifiedSince: optionalHeader(h["if-unmodified-since"]),
    ifModifiedSince: optionalHeader(h["if-modified-since"]),
    idempotencyKey: optionalHeader(h["idempotency-key"]),
  };
}

function optionalHeader(value: string | string[] | undefined): string | undefined {
  if (value === undefined) return undefined;
  return Array.isArray(value) ? value.join(", ") : value;
}

let requestCounter = 0;

function correlationOf(req: FastifyRequest): { runId: string; clientId: string; requestId: string } {
  const h = req.headers;
  const requestId =
    optionalHeader(h["x-request-id"]) ??
    req.id ??
    `req-${process.pid}-${++requestCounter}-${Date.now().toString(36)}`;
  return {
    runId: optionalHeader(h["x-run-id"]) ?? "uncorrelated",
    clientId: optionalHeader(h["x-client-id"]) ?? "anonymous",
    requestId,
  };
}

function errorBody(category: string, message: string, detail: string | undefined, reqId: string) {
  return {
    error: {
      category,
      message,
      ...(detail ? { detail } : {}),
      requestId: reqId,
    },
  };
}

function describeSteps(steps: EvalStep[]): string[] {
  return steps.map((step): string => {
    switch (step.stage) {
      case "if-unmodified-since":
      case "if-modified-since": {
        const ignored =
          step.result === "ignored-malformed" ||
          step.result === "ignored-because-if-none-match" ||
          step.result === "ignored-method";
        const dateInfo = ignored ? step.result : `parsed=${step.parsedMs ?? "<unparsed>"}`;
        return `${step.stage} supplied=${step.suppliedDate} ${dateInfo} observed=${
          step.observedUpdatedAtMs ?? "<absent>"
        } => ${step.result}`;
      }
      case "if-match":
      case "if-none-match": {
        const cand = step.candidates
          .map((c) => `${c.raw}${c.matched ? "=>MATCH" : ""}`)
          .join(", ");
        return `${step.stage} [${step.comparison}] observed=${step.observed ?? "<absent>"} ${
          step.star ? "star" : cand
        } => ${step.result}`;
      }
    }
  });
}

export async function buildApp(deps: BuildAppDeps): Promise<FastifyInstance> {
  const app = createFastify({
    // Info-level request lines are suppressed unless diagnostics opts in.
    logger: deps.diagnostics ? { level: "info" } : { level: "warn" },
  });

  const { kernel, store } = deps;

  function logOutcome(req: FastifyRequest, outcome: KernelOutcome): void {
    if (!deps.diagnostics) return;
    const r = outcome.record;
    const line = {
      msg: "conditional decision",
      runId: r.runId,
      clientId: r.clientId,
      requestId: r.requestId,
      method: r.method,
      resource: r.resourceId,
      observedVersion: r.observedVersion,
      observedEtag: r.observedEtag,
      verdict: r.verdict,
      failureCategory: r.failureCategory,
      status: r.outcomeStatus,
      resultingVersion: r.resultingVersion,
      resultingEtag: r.resultingEtag,
      replayed: r.replayed,
      steps: describeSteps(r.steps),
      path: req.url,
    };
    req.log.info(line);
  }

  function send(outcome: KernelOutcome, reply: FastifyReply, req: FastifyRequest, includeBody: boolean): FastifyReply {
    logOutcome(req, outcome);
    const corr = correlationOf(req);
    reply.header("x-request-id", corr.requestId);

    switch (outcome.kind) {
      case "present": {
        reply.code(outcome.status);
        reply.header("ETag", outcome.snapshot.etagHeader);
        reply.header("Last-Modified", formatHttpDate(outcome.snapshot.updatedAtMs));
        reply.header("X-Resource-Version", String(outcome.snapshot.version));
        reply.header("Content-Type", outcome.snapshot.contentType);
        if (outcome.replayed) reply.header("Idempotent-Replay", "true");
        return includeBody ? reply.send(outcome.snapshot.body) : reply.send();
      }
      case "not-modified": {
        reply.code(304);
        reply.header("ETag", outcome.snapshot.etagHeader);
        reply.header("Last-Modified", formatHttpDate(outcome.snapshot.updatedAtMs));
        reply.header("X-Resource-Version", String(outcome.snapshot.version));
        return reply.send();
      }
      case "no-content": {
        reply.code(204);
        if (outcome.record.resultingEtag) {
          reply.header("ETag", renderETag(outcome.record.resultingEtag, deps.etagStrength));
        }
        if (outcome.replayed) reply.header("Idempotent-Replay", "true");
        return reply.send();
      }
      case "not-found":
        return reply
          .code(404)
          .send(errorBody("not-found", `Resource ${outcome.record.resourceId} not found`, undefined, corr.requestId));
      case "precondition-failed":
        return reply.code(412).send(
          errorBody(
            outcome.category,
            "Precondition failed",
            outcome.detail,
            corr.requestId,
          ),
        );
      case "malformed":
        return reply.code(400).send(
          errorBody(outcome.category, "Malformed conditional header", outcome.detail, corr.requestId),
        );
      case "replay-conflict":
        reply.header("Retry-After", "0");
        return reply
          .code(409)
          .send(errorBody(outcome.category, "Idempotency key reused with a different request", outcome.detail, corr.requestId));
      case "busy":
        reply.header("Retry-After", "1");
        return reply
          .code(503)
          .send(errorBody(outcome.category, "Storage temporarily locked; retry", outcome.detail, corr.requestId));
    }
  }

  app.addContentTypeParser(
    ["text/plain", "application/json", "application/octet-stream"],
    { parseAs: "string" },
    (_req, payload, done) => done(null, payload),
  );
  // Tolerate clients that PUT a body without a Content-Type rather than
  // answering 415; bytes are stored verbatim and labeled on retrieval.
  app.addContentTypeParser("*", { parseAs: "string" }, (_req, payload, done) =>
    done(null, payload),
  );

  // ---- resource routes --------------------------------------------------

  app.get(RESOURCE_ROUTE, async (req, reply) => {
    const id = resourceIdOf(req);
    // Fastify routes HEAD requests to the GET handler and suppresses the body.
    const outcome = kernel.get({
      resourceId: id,
      conditions: extractConditions(req),
      correlation: correlationOf(req),
    });
    return send(outcome, reply, req, req.method !== "HEAD");
  });

  app.put(RESOURCE_ROUTE, async (req, reply) => {
    const id = resourceIdOf(req);
    const body = typeof req.body === "string" ? req.body : req.body === undefined ? "" : JSON.stringify(req.body);
    const outcome = kernel.put({
      resourceId: id,
      conditions: extractConditions(req),
      correlation: correlationOf(req),
      body,
      contentType: optionalHeader(req.headers["content-type"]) ?? "text/plain; charset=utf-8",
    });
    return send(outcome, reply, req, true);
  });

  app.delete(RESOURCE_ROUTE, async (req, reply) => {
    const id = resourceIdOf(req);
    const outcome = kernel.delete({
      resourceId: id,
      conditions: extractConditions(req),
      correlation: correlationOf(req),
    });
    return send(outcome, reply, req, false);
  });

  // ---- diagnostics interface -------------------------------------------

  app.get("/health", async () => ({ status: "ok", store: store.kind }));

  app.get("/diagnostics/decisions", async (req, reply) => {
    const q = req.query as { runId?: string; clientId?: string; resourceId?: string };
    const records = store.listDecisions({
      runId: q.runId,
      clientId: q.clientId,
      resourceId: q.resourceId,
    });
    return reply.send({
      count: records.length,
      records: records.map((r) => ({
        ...r,
        stepsReadable: describeSteps(r.steps),
      })),
    });
  });

  app.get("/diagnostics/history", async (req, reply) => {
    const q = req.query as { resourceId?: string };
    if (!q.resourceId) {
      return reply.code(400).send({
        error: { category: "malformed", message: "query parameter resourceId is required" },
      });
    }
    const history = store.listHistory(q.resourceId);
    return reply.send({ resourceId: q.resourceId, versions: history });
  });

  app.setErrorHandler((err: Error & { statusCode?: number; code?: string }, _req, reply) => {
    app.log.error({ err }, "unhandled error");
    // Preserve transport-level client errors (415 unsupported media type,
    // 400 bad JSON, …) instead of collapsing them to a 500 false failure.
    const status =
      typeof err.statusCode === "number" && err.statusCode >= 400 && err.statusCode < 500
        ? err.statusCode
        : 500;
    reply.code(status).send({
      error: {
        category: status === 500 ? "internal-error" : "bad-request",
        message: err.message,
      },
    });
  });

  return app;
}
