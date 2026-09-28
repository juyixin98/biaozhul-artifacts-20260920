/**
 * Fastify transport: HTTP <-> kernel.
 *
 * The body is consumed as RAW TEXT rather than pre-parsed JSON, because the
 * JSON-RPC spec requires a parse_error (-32700) for malformed payloads and a
 * framework-level 400 would erase that distinction.
 *
 * A request-scoped AbortController is tied to the socket: if the client
 * disconnects mid-flight the kernel learns via its signal, marks still-pending
 * work interrupted and reports the request as undecidable.
 */

import Fastify, {
  type FastifyInstance,
  type FastifyReply,
  type FastifyRequest,
} from "fastify";

import { loadConfig, type AppConfig } from "../config.js";
import { DiagnosticsQueries } from "../diagnostics/queries.js";
import type { DiagnosticsLogger } from "../diagnostics/logger.js";
import { Kernel } from "../kernel/engine.js";
import { parseRpcPayload } from "../protocol/parser.js";
import type { LedgerStore } from "../state/store.js";

export interface AppDeps {
  readonly config: AppConfig;
  readonly store: LedgerStore;
  readonly kernel: Kernel;
  readonly logger: DiagnosticsLogger;
}

export function buildApp(deps: AppDeps): FastifyInstance {
  const app = Fastify({
    logger: false,
    bodyLimit: 1_048_576,
  });
  const diagnostics = new DiagnosticsQueries(deps.store);

  // Keep the raw bytes: parse_error must be produced by OUR parser.
  app.addContentTypeParser(
    "application/json",
    { parseAs: "string" },
    (_req, body: string, done) => {
      done(null, body);
    },
  );

  app.get("/healthz", async () => ({
    ok: true,
    service: "jsonrpc-batch",
  }));

  app.post("/", async (req: FastifyRequest, reply: FastifyReply) => {
    const raw = typeof req.body === "string" ? req.body : "";
    const outcome = parseRpcPayload(raw);
    const controller = new AbortController();
    // NOTE: listen on the SOCKET, not the request. In modern Node the
    // IncomingMessage "close" event fires once the request body is consumed,
    // which happens before/while we process — it does NOT mean the client
    // disconnected. Socket "close" plus the writableFinished guard does.
    const socket = req.raw.socket;
    const onClose = (): void => {
      if (!reply.raw.writableFinished) controller.abort();
    };
    socket.on("close", onClose);

    let result;
    try {
      result = await deps.kernel.handle(outcome, {
        signal: controller.signal,
        sizeBytes: Buffer.byteLength(raw),
      });
    } finally {
      socket.removeListener("close", onClose);
    }

    if (reply.raw.destroyed || reply.raw.writableEnded) {
      return; // client gone; kernel already recorded undecidable/interrupted
    }

    switch (result.kind) {
      case "single":
        return reply.code(result.httpStatus).send(result.response);
      case "batch":
        return reply.code(200).send(result.responses);
      case "notificationOnly":
        return reply.code(204).send();
      case "topLevelError":
        return reply.code(result.httpStatus).send(result.response);
      case "interrupted":
        // The client is gone and the outcome is undecidable. Do NOT emit an
        // HTTP status: destroy the socket so the caller observes a transport
        // failure rather than a response it never asked to wait for.
        reply.raw.destroy();
        return;
    }
  });

  /* ----------------------------- diagnostics ----------------------------- */

  app.get("/diag/requests", async (req: FastifyRequest, reply: FastifyReply) => {
    const limit = clampLimit((req.query as { limit?: string }).limit);
    return reply.code(200).send({
      requests: diagnostics.listRequests(limit),
    });
  });

  app.get(
    "/diag/requests/:corr",
    async (req: FastifyRequest, reply: FastifyReply) => {
      const corr = (req.params as { corr: string }).corr;
      const view = diagnostics.getRequest(corr);
      if (!view) {
        return reply.code(404).send({
          error: "not_found",
          message: `unknown request correlation id: ${corr}`,
        });
      }
      return reply.code(200).send(view);
    },
  );

  app.get(
    "/diag/operations",
    async (req: FastifyRequest, reply: FastifyReply) => {
      const query = req.query as {
        status?: string;
        kind?: string;
        limit?: string;
      };
      return reply.code(200).send({
        operations: diagnostics.listOperations({
          status: query.status,
          kind: query.kind,
          limit: clampLimit(query.limit),
        }),
      });
    },
  );

  app.get(
    "/diag/operations/:seq",
    async (req: FastifyRequest, reply: FastifyReply) => {
      const seq = Number((req.params as { seq: string }).seq);
      if (!Number.isInteger(seq) || seq < 1) {
        return reply.code(400).send({
          error: "invalid_request",
          message: "operation sequence must be a positive integer",
        });
      }
      const view = diagnostics.getOperation(seq);
      if (!view) {
        return reply.code(404).send({
          error: "not_found",
          message: `unknown operation sequence: ${seq}`,
        });
      }
      return reply.code(200).send(view);
    },
  );

  return app;
}

function clampLimit(raw: string | undefined): number {
  if (raw === undefined) return 50;
  const n = Number(raw);
  if (!Number.isFinite(n)) return 50;
  return Math.min(Math.max(Math.trunc(n), 1), 500);
}

export function createApp(deps: Omit<AppDeps, "config"> & { config?: AppConfig }) {
  const config = deps.config ?? loadConfig();
  return buildApp({ ...deps, config });
}
