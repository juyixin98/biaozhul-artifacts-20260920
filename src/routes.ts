/**
 * HTTP API — JSON in/out, local-only verification. No endpoint ever makes a
 * network call; content must be supplied as local blocks first.
 *
 *   POST   /api/v1/blocks                     ingest one content-addressed block
 *   POST   /api/v1/verify/cid/:cid            verify a stored block on its own
 *   POST   /api/v1/verify/metadata            full metadata+media DAG verification
 *   GET    /api/v1/blocks/:cid                block info / raw bytes
 *   PUT    /api/v1/tokens/:tokenId/metadata   propose a metadata revision (chain tx)
 *   GET    /api/v1/tokens/:tokenId            current effective metadata + history
 *   POST   /api/v1/chain/tip                  advance the simulated chain tip
 *   POST   /api/v1/chain/reorg                report a reorg (undoes unconfirmed)
 *   GET    /api/v1/events                     audit ledger
 *   GET    /healthz
 */
import type { FastifyInstance, FastifyPluginAsync } from 'fastify';
import { z } from './zod-lite.js';
import type { Store } from './store.js';
import type { Config } from './config.js';
import { Verifier } from './verify/verifier.js';
import { ChainService } from './chain-service.js';
import { parseCID, cidToString } from './codec/cid.js';
import { AppError } from './errors.js';

const TOKEN_ID_RE = /^[A-Za-z0-9:._-]{1,128}$/;
const HASH_RE = /^0x[0-9a-fA-F]{8,64}$/;

export interface Services {
  store: Store;
  verifier: Verifier;
  chain: ChainService;
  config: Config;
}

function base64ToBytes(b64: string, field = 'dataBase64'): Uint8Array {
  const compact = b64.replace(/\s+/g, '');
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(compact) || compact.length % 4 !== 0) {
    throw new AppError('E_BAD_REQUEST', `${field} is not valid standard base64`);
  }
  return Uint8Array.from(Buffer.from(compact, 'base64'));
}

export const apiRoutes: FastifyPluginAsync<{ services: Services }> = async (fastify, opts) => {
  const { store, verifier, chain, config } = opts.services;

  // ----------------------------------------------------------- blocks
  fastify.post('/blocks', async (req, reply) => {
    const body = z.object({
      cid: z.string(),
      dataBase64: z.string(),
    }).parse(req.body);

    let cid;
    try {
      cid = parseCID(body.cid);
    } catch (e) {
      throw new AppError('E_UNSUPPORTED_CID', (e as Error).message);
    }
    const data = base64ToBytes(body.dataBase64);
    if (data.length > config.maxBlockSize) {
      throw new AppError('E_BLOCK_TOO_LARGE', `block ${data.length} bytes exceeds MAX_BLOCK_SIZE ${config.maxBlockSize}`, 413);
    }
    const report = verifier.verifyBlock(cid, data);
    if (!report.ok) {
      return reply.code(422).send({ ok: false, code: 'E_BLOCK_INVALID', report });
    }
    const inserted = store.putBlock(cid, cid.codecName, data);
    return reply.code(inserted ? 201 : 200).send({
      ok: true,
      inserted,
      cid: cidToString(cid),
      codec: cid.codecName,
      size: data.length,
      report,
    });
  });

  fastify.get('/blocks/:cid', async (req, reply) => {
    const params = req.params as { cid: string };
    const raw = String((req.query as { raw?: string }).raw ?? '');
    let cid;
    try {
      cid = parseCID(params.cid);
    } catch (e) {
      throw new AppError('E_UNSUPPORTED_CID', (e as Error).message);
    }
    const row = store.getBlock(cid);
    if (!row) throw new AppError('E_NOT_FOUND', `block ${params.cid} not found`, 404);
    if (raw === '1') {
      reply.header('content-type', 'application/octet-stream');
      reply.header('x-ipld-codec', row.codec);
      reply.header('x-content-cid', row.cid);
      return reply.send(Buffer.from(row.data));
    }
    return {
      cid: row.cid,
      codec: row.codec,
      size: row.size,
      storedAt: row.stored_at,
      report: verifier.verifyBlock(cid, Uint8Array.from(row.data)),
    };
  });

  // ----------------------------------------------------------- verify
  fastify.post('/verify/cid/:cid', async (req) => {
    const params = req.params as { cid: string };
    let cid;
    try {
      cid = parseCID(params.cid);
    } catch (e) {
      throw new AppError('E_UNSUPPORTED_CID', (e as Error).message);
    }
    const row = store.getBlock(cid);
    if (!row) throw new AppError('E_NOT_FOUND', `block ${params.cid} not found; ingest it via POST /blocks`, 404);
    const report = verifier.verifyBlock(cid, Uint8Array.from(row.data));
    return { ok: report.ok, report };
  });

  fastify.post('/verify/metadata', async (req, reply) => {
    const body = z
      .object({ cid: z.string() })
      .parse(req.body);
    // Validate CID syntax up front for a clean 400; verification reports 200
    // with ok=false for content-level failures so callers get full evidence.
    try {
      parseCID(body.cid);
    } catch (e) {
      throw new AppError('E_UNSUPPORTED_CID', (e as Error).message);
    }
    const report = verifier.verifyMetadataCid(body.cid);
    return reply.code(report.ok ? 200 : 422).send({ ok: report.ok, report });
  });

  // ----------------------------------------------------------- tokens
  fastify.put('/tokens/:tokenId/metadata', async (req, reply) => {
    const { tokenId } = req.params as { tokenId: string };
    if (!TOKEN_ID_RE.test(tokenId)) {
      throw new AppError('E_BAD_REQUEST', 'tokenId must be 1-128 chars of [A-Za-z0-9:._-]');
    }
    const body = z
      .object({
        cid: z.string(),
        height: z.intNonneg(),
        blockHash: z.regex(HASH_RE, 'blockHash must be 0x + 8-64 hex'),
      })
      .parse(req.body);

    let cidText: string;
    try {
      cidText = cidToString(parseCID(body.cid));
    } catch (e) {
      throw new AppError('E_UNSUPPORTED_CID', (e as Error).message);
    }

    // Replay protection: the same live (token, CID) is a duplicate revision.
    // A CID whose earlier proposal was reorged MAY be proposed again.
    const existing = store.getVersionByCid(tokenId, cidText);
    if (existing && existing.status !== 'reorged') {
      throw new AppError(
        'E_CONFLICT',
        `CID ${cidText} is already recorded as v${existing.version} (status=${existing.status}); metadata revisions are append-only`,
        409,
        { version: existing.version, status: existing.status },
      );
    }
    const pending = store.hasPendingProposal(tokenId);
    if (pending) {
      throw new AppError(
        'E_CONFLICT',
        `token has an unconfirmed proposal v${pending.version}; a reorg must settle it before a new revision`,
        409,
        { pendingVersion: pending.version },
      );
    }

    // The revision must actually verify against the local block set.
    const report = verifier.verifyMetadataCid(cidText);
    if (!report.ok) {
      return reply.code(422).send({ ok: false, code: 'E_METADATA_INVALID', report });
    }

    const tip = chain.getTip();
    if (tip && body.height < tip.height - config.confirmations) {
      throw new AppError('E_BAD_REQUEST', `height ${body.height} is already beyond the confirmation window (tip ${tip.height})`);
    }

    const row = store.proposeVersion({ tokenId, cid: cidText, height: body.height, blockHash: body.blockHash });
    store.logEvent({
      type: 'PROPOSE',
      token_id: tokenId,
      version: row.version,
      cid: cidText,
      height: body.height,
      block_hash: body.blockHash,
      detail: `metadata v${row.version} proposed`,
    });

    // Convenience: if already deep enough (N blocks on top), confirm now.
    let confirmed = false;
    if (tip && body.height <= tip.height - config.confirmations) {
      store.supersedeConfirmedFor(tokenId);
      store.confirmVersion(row.id, tip.height, tip.blockHash);
      confirmed = true;
    }

    return reply.code(201).send({
      ok: true,
      tokenId,
      version: row.version,
      status: confirmed ? 'confirmed' : 'proposed',
      cid: cidText,
      proposedAt: { height: body.height, blockHash: body.blockHash },
      confirmationsRequired: config.confirmations,
      verification: report,
    });
  });

  fastify.get('/tokens/:tokenId', async (req) => {
    const { tokenId } = req.params as { tokenId: string };
    const versions = store.listVersions(tokenId);
    if (versions.length === 0) throw new AppError('E_NOT_FOUND', `token ${tokenId} has no metadata`, 404);
    const current = versions.filter((v) => v.status === 'confirmed').at(-1) ?? null;
    const pending = versions.find((v) => v.status === 'proposed') ?? null;
    return {
      tokenId,
      current: current
        ? {
            version: current.version,
            cid: current.cid,
            confirmedHeight: current.confirmed_height,
            effectiveHeight: current.effective_height,
            effectiveBlockHash: current.effective_block_hash,
          }
        : null,
      pending: pending
        ? {
            version: pending.version,
            cid: pending.cid,
            proposedHeight: pending.proposed_height,
            proposedBlockHash: pending.proposed_block_hash,
          }
        : null,
      history: versions.map((v) => ({
        version: v.version,
        cid: v.cid,
        status: v.status,
        proposedHeight: v.proposed_height,
        proposedBlockHash: v.proposed_block_hash,
        confirmedHeight: v.confirmed_height,
        effectiveHeight: v.effective_height,
        effectiveBlockHash: v.effective_block_hash,
      })),
    };
  });

  // ----------------------------------------------------------- chain
  fastify.post('/chain/tip', async (req) => {
    const body = z
      .object({ height: z.intNonneg(), blockHash: z.regex(HASH_RE, 'blockHash must be 0x + 8-64 hex') })
      .parse(req.body);
    try {
      const r = chain.advanceTip(body.height, body.blockHash);
      return {
        ok: true,
        tip: r.tip,
        confirmed: r.confirmed.map((c) => ({ tokenId: c.token_id, version: c.version, cid: c.cid })),
        confirmations: config.confirmations,
      };
    } catch (e) {
      const code = (e as NodeJS.ErrnoException).code;
      throw new AppError(code === 'E_CONFLICT' ? 'E_CONFLICT' : 'E_BAD_REQUEST', (e as Error).message, code === 'E_CONFLICT' ? 409 : 400);
    }
  });

  fastify.post('/chain/reorg', async (req) => {
    const body = z
      .object({
        height: z.intNonneg(),
        oldHash: z.string(),
        newHash: z.regex(HASH_RE, 'newHash must be 0x + 8-64 hex'),
      })
      .parse(req.body);
    try {
      const r = chain.applyReorg(body.height, body.oldHash, body.newHash);
      return {
        ok: true,
        reverted: r.reverted.map((v) => ({ tokenId: v.token_id, version: v.version, cid: v.cid })),
        protectedConfirmed: r.protected.map((v) => ({ tokenId: v.token_id, version: v.version, cid: v.cid })),
        note: 'only unconfirmed updates are auto-reverted; confirmed versions keep the previous CID effective',
      };
    } catch (e) {
      throw new AppError('E_BAD_REQUEST', (e as Error).message);
    }
  });

  // ----------------------------------------------------------- events
  fastify.get('/events', async (req) => {
    const q = z.object({ limit: z.intInRange(1, 500, 100) }).safeParse(req.query ?? {}).value ?? { limit: 100 };
    return { events: store.listEvents(q.limit) };
  });
};

export function registerErrorHandler(app: FastifyInstance): void {
  app.setErrorHandler((error, req, reply) => {
    if (error instanceof AppError) {
      req.log.warn(error.message);
      return reply.code(error.statusCode).send({ ok: false, code: error.code, error: error.message, details: error.details });
    }
    if ((error as { validation?: unknown }).validation) {
      return reply.code(400).send({ ok: false, code: 'E_VALIDATION', error: error.message });
    }
    req.log.error(error);
    return reply.code(500).send({ ok: false, code: 'E_INTERNAL', error: error.message });
  });
}
