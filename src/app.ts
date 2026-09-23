import Fastify, { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";
import { LIMITS } from "./config";
import { AppError } from "./errors";
import { BlockService, decodeBase64Strict } from "./services/block-service";
import { BlockStore } from "./store/blockstore";
import { ChainStore, RevisionStatus } from "./store/chainstore";
import { DagVerifier, parseCidOrThrow } from "./verifier";
import Database from "better-sqlite3";
import { openDatabase } from "./db";

export interface ServerDeps {
  db: Database.Database;
  blocks: BlockStore;
  chain: ChainStore;
  blockService: BlockService;
  verifier: DagVerifier;
}

export function createDeps(dbPath: string): ServerDeps {
  const db = openDatabase(dbPath);
  const blocks = new BlockStore(db);
  const chain = new ChainStore(db);
  const blockService = new BlockService(blocks);
  const verifier = new DagVerifier(blocks);
  return { db, blocks, chain, blockService, verifier };
}

export function buildServer(deps: ServerDeps, logLevel = "info"): FastifyInstance {
  const app = Fastify({
    logger: { level: logLevel },
    bodyLimit: LIMITS.MAX_BLOCK_BYTES + 64 * 1024,
  });
  const { blocks, chain, blockService, verifier } = deps;

  app.setErrorHandler((err, req, reply) => {
    if (err instanceof AppError) {
      void reply.status(err.statusCode).send({
        error: err.code,
        message: err.message,
        ...(err.details !== undefined ? { details: err.details } : {}),
      });
      return;
    }
    // fastify validation errors
    if ((err as { validation?: unknown }).validation) {
      void reply.status(400).send({ error: "BAD_REQUEST", message: err.message });
      return;
    }
    req.log.error(err);
    void reply.status(500).send({ error: "INTERNAL", message: "internal server error" });
  });

  app.get("/health", async () => ({ ok: true, storedBlocks: blocks.count() }));

  // ----------------------------------------------------------- blocks --

  app.put("/blocks/:cid", async (req: FastifyRequest, reply: FastifyReply) => {
    const cid = (req.params as { cid: string }).cid;
    const body = req.body as { data_base64?: unknown };
    if (!body || typeof body.data_base64 !== "string") {
      throw new AppError("BAD_REQUEST", "body must be { data_base64: string }");
    }
    const data = decodeBase64Strict(body.data_base64);
    const result = blockService.ingest(cid, data);
    return reply.status(result.existed ? 200 : 201).send(result);
  });

  app.get("/blocks", async () => ({ blocks: blocks.list() }));

  app.get("/blocks/:cid", async (req) => {
    const cid = parseCidOrThrow((req.params as { cid: string }).cid, "path cid").canonical;
    const rec = blocks.getRecord(cid);
    if (!rec) {
      throw new AppError("BLOCK_NOT_FOUND", `no block stored for ${cid}`, { cid });
    }
    return {
      cid: rec.cid,
      version: rec.version,
      codec: rec.codec,
      hashCode: rec.hashCode,
      size: rec.size,
    };
  });

  // ------------------------------------------------------------ verify --

  app.post("/verify", async (req) => {
    const body = req.body as { root_cid?: unknown; expected_version?: unknown };
    if (!body || typeof body.root_cid !== "string") {
      throw new AppError("BAD_REQUEST", "body must be { root_cid: string, expected_version?: number }");
    }
    const expectedVersion =
      body.expected_version === undefined
        ? undefined
        : typeof body.expected_version === "number" &&
          Number.isInteger(body.expected_version)
        ? body.expected_version
        : undefined;
    const result = verifier.verifyRoot(body.root_cid, expectedVersion);
    return result;
  });

  // ------------------------------------------------------------- chain --

  app.post("/chain/blocks", async (req) => {
    const body = req.body as
      | { action?: unknown; block_hash?: unknown; parent_hash?: unknown; from_height?: unknown; new_blocks?: unknown };
    if (body?.action === "append") {
      if (typeof body.block_hash !== "string" || !/^0x[0-9a-fA-F]{8,128}$/.test(body.block_hash)) {
        throw new AppError("BAD_REQUEST", "block_hash must be a 0x-prefixed hex string");
      }
      const parent =
        body.parent_hash === null
          ? null
          : typeof body.parent_hash === "string"
          ? body.parent_hash
          : undefined;
      if (parent !== null && parent !== undefined && !/^0x[0-9a-fA-F]{8,128}$/.test(parent)) {
        throw new AppError("BAD_REQUEST", "parent_hash must be a 0x-prefixed hex string or null");
      }
      const row = chain.appendBlock(body.block_hash, parent ?? null);
      return { action: "append", block: row };
    }
    if (body?.action === "reorg") {
      if (typeof body.from_height !== "number" || !Number.isInteger(body.from_height)) {
        throw new AppError("BAD_REQUEST", "from_height must be an integer");
      }
      if (!Array.isArray(body.new_blocks)) {
        throw new AppError("BAD_REQUEST", "new_blocks must be an array of {hash, parent}");
      }
      const newBlocks = body.new_blocks.map(
        (b: unknown, i: number): { hash: string; parent: string | null } => {
          const o = b as { hash?: unknown; parent?: unknown };
          if (
            !o ||
            typeof o.hash !== "string" ||
            !/^0x[0-9a-fA-F]{8,128}$/.test(o.hash)
          ) {
            throw new AppError("BAD_REQUEST", `new_blocks[${i}].hash invalid`);
          }
          if (o.parent !== null && (typeof o.parent !== "string" || !/^0x[0-9a-fA-F]{8,128}$/.test(o.parent))) {
            throw new AppError("BAD_REQUEST", `new_blocks[${i}].parent must be hex or null`);
          }
          return { hash: o.hash, parent: o.parent };
        }
      );
      chain.reorganize(body.from_height, newBlocks);
      return { action: "reorg", fromHeight: body.from_height, newTip: chain.tip() };
    }
    throw new AppError("BAD_REQUEST", "action must be 'append' or 'reorg'");
  });

  app.get("/chain", async () => {
    const tip = chain.tip();
    return {
      tipHeight: tip ? tip.height : -1,
      tip: tip,
      blocks: chain.listBlocks(),
      requiredConfirmations: LIMITS.REQUIRED_CONFIRMATIONS,
    };
  });

  // ------------------------------------------------------------ tokens --

  app.put("/tokens/:tokenId", async (req, reply) => {
    const tokenId = (req.params as { tokenId: string }).tokenId;
    if (!/^[A-Za-z0-9._:/-]{1,128}$/.test(tokenId)) {
      throw new AppError("BAD_REQUEST", "tokenId must be 1..128 chars [A-Za-z0-9._:/-]");
    }
    const body = req.body as { root_cid?: unknown };
    if (!body || typeof body.root_cid !== "string") {
      throw new AppError("BAD_REQUEST", "body must be { root_cid: string }");
    }
    const tip = chain.tip();
    if (!tip) throw new AppError("BAD_REQUEST", "chain is empty; POST /chain/blocks first");

    const expectedVersion = chain.highestVersion(tokenId);
    const nextVersion = expectedVersion === null ? 1 : expectedVersion + 1;

    // Full real verification; metadata "version" must match the next revision.
    const evidence = verifier.verifyRoot(body.root_cid, nextVersion);

    const rootCid = parseCidOrThrow(body.root_cid).canonical;
    const rev = chain.registerRevision({
      tokenId,
      version: nextVersion,
      cid: rootCid,
      anchorHeight: tip.height,
      anchorHash: tip.blockHash,
      evidence,
    });
    return reply.status(201).send(serializeRevision(rev, chain.confirmations(rev.anchorHeight)));
  });

  app.get("/tokens", async () => {
    const tokens = chain.listTokens();
    return {
      tokens: tokens.map((t) => {
        const revs = chain.listRevisions(t.tokenId);
        const latest = revs[revs.length - 1];
        return {
          tokenId: t.tokenId,
          currentCid: t.currentCid,
          latestStatus: latest?.status ?? null,
          confirmations: latest ? chain.confirmations(latest.anchorHeight) : 0,
        };
      }),
    };
  });

  app.get("/tokens/:tokenId", async (req) => {
    const tokenId = (req.params as { tokenId: string }).tokenId;
    const token = chain.getToken(tokenId);
    if (!token) throw new AppError("NOT_FOUND", `token '${tokenId}' not found`);
    const revs = chain.listRevisions(tokenId);
    const live = revs.filter((r) => r.status !== "revoked");
    const current = live[live.length - 1] ?? null;
    return {
      tokenId,
      currentCid: token.currentCid,
      currentStatus: current?.status ?? null,
      currentVersion: current?.version ?? null,
      confirmations: current ? chain.confirmations(current.anchorHeight) : 0,
      requiredConfirmations: LIMITS.REQUIRED_CONFIRMATIONS,
    };
  });

  app.get("/tokens/:tokenId/history", async (req) => {
    const tokenId = (req.params as { tokenId: string }).tokenId;
    if (!chain.getToken(tokenId)) {
      throw new AppError("NOT_FOUND", `token '${tokenId}' not found`);
    }
    const revs = chain.listRevisions(tokenId);
    return {
      tokenId,
      revisions: revs.map((r) => serializeRevision(r, chain.confirmations(r.anchorHeight))),
    };
  });

  app.get("/tokens/:tokenId/revisions/:version/evidence", async (req) => {
    const { tokenId, version } = req.params as { tokenId: string; version: string };
    const v = Number(version);
    if (!Number.isInteger(v)) throw new AppError("BAD_REQUEST", "version must be an integer");
    const rev = chain.getRevision(tokenId, v);
    if (!rev) throw new AppError("NOT_FOUND", `revision ${v} not found`);
    return {
      tokenId,
      version: rev.version,
      cid: rev.cid,
      status: rev.status,
      anchorHeight: rev.anchorHeight,
      anchorHash: rev.anchorHash,
      confirmations: chain.confirmations(rev.anchorHeight),
      evidence: JSON.parse(rev.evidence),
    };
  });

  return app;
}

function serializeRevision(
  r: import("./store/chainstore").RevisionRow,
  confirmations: number
): Record<string, unknown> {
  const finalized = confirmations >= LIMITS.REQUIRED_CONFIRMATIONS;
  const status: RevisionStatus = r.status;
  return {
    tokenId: r.tokenId,
    version: r.version,
    cid: r.cid,
    status,
    anchorHeight: r.anchorHeight,
    anchorHash: r.anchorHash,
    confirmations,
    requiredConfirmations: LIMITS.REQUIRED_CONFIRMATIONS,
    finalized,
    createdAt: new Date(r.createdAt).toISOString(),
  };
}

