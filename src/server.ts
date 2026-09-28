/**
 * 传输层（Fastify）：只做 HTTP 适配——解析头、调用内核、按 Resolution 读字节、
 * 设置状态码与头、记录诊断。所有判定逻辑都在内核，路由保持薄。
 */
import Fastify, { type FastifyInstance, type FastifyReply, type FastifyRequest } from 'fastify';
import { randomUUID } from 'node:crypto';
import type { OutgoingHttpHeaders } from 'node:http';
import { parseIfRange } from './contract/ifRange.js';
import { parseRangeHeader } from './contract/rangeHeader.js';
import { sanitizeHeaders } from './diagnostics/redact.js';
import { DecisionLog, type DecisionRecord } from './diagnostics/decisionLog.js';
import { formatImfFixdate } from './kernel/httpDate.js';
import {
  encodeClosing,
  encodePartHeader,
  encodePartTrailer,
  generateBoundary,
} from './kernel/multipart.js';
import { resolveRangeRequest } from './kernel/resolve.js';
import type {
  FullResolution,
  ObjectMeta,
  PartialResolution,
  RejectedResolution,
  Resolution,
  ResolutionSummary,
} from './kernel/types.js';
import { SqliteObjectStore } from './store/sqliteStore.js';

export interface BuildServerOptions {
  readonly store: SqliteObjectStore;
  readonly maxRanges: number;
  readonly maxResponseBytes: number;
  readonly diagnosticsCapacity: number;
}

interface ErrorBody {
  readonly success: false;
  readonly error: { readonly code: string; readonly message: string; readonly requestId: string };
}

type RecordDecision = (
  statusCode: number,
  outcome: DecisionRecord['outcome'],
  reason: string | null,
  detail: string | null,
  meta: ObjectMeta | null,
  resolution: Resolution | null,
  entityBytes: bigint | null,
) => void;

interface RequestContext {
  readonly req: FastifyRequest;
  readonly reply: FastifyReply;
  readonly store: SqliteObjectStore;
  readonly requestId: string;
  readonly isHead: boolean;
  readonly record: RecordDecision;
}

function errorBody(code: string, message: string, requestId: string): ErrorBody {
  return { success: false, error: { code, message, requestId } };
}

function summaryToRecord(summary: ResolutionSummary | null) {
  if (!summary) {
    return {
      parsedSpecCount: null,
      satisfiableCount: null,
      unsatisfiableCount: null,
      unsatisfiableReasons: [] as readonly string[],
      plannedTotalBytes: null,
    };
  }
  return {
    parsedSpecCount: summary.parsedSpecCount,
    satisfiableCount: summary.satisfiableCount,
    unsatisfiableCount: summary.unsatisfiableCount,
    unsatisfiableReasons: summary.unsatisfiableReasons,
    plannedTotalBytes: summary.totalBytes === null ? null : summary.totalBytes.toString(),
  };
}

/**
 * HEAD：头必须按“计划实体长度”给出，但不发送体。
 * hijack 后绕过 Fastify 序列化（否则它会按实际发送的空体重写 Content-Length），
 * 并手动 writeHead 把此前通过 reply.header 设置的实体头原样写出。
 */
function endHeadWithoutBody(reply: FastifyReply): void {
  reply.hijack();
  reply.raw.writeHead(reply.statusCode, reply.getHeaders() as OutgoingHttpHeaders);
  reply.raw.end();
}

function setRepresentationHeaders(reply: FastifyReply, meta: ObjectMeta): void {
  reply.header('ETag', meta.etag);
  reply.header('Last-Modified', formatImfFixdate(meta.lastModifiedMs));
  reply.header('Accept-Ranges', 'bytes');
}

/** 200 完整表示。 */
function respondFull(ctx: RequestContext, meta: ObjectMeta, resolution: FullResolution): FastifyReply {
  const { reply, store, isHead, record } = ctx;
  reply.code(200);
  reply.header('Content-Type', meta.contentType);
  reply.header('Content-Length', meta.size.toString());
  if (isHead) {
    endHeadWithoutBody(reply);
    record(200, 'full', resolution.reason, null, meta, resolution, meta.size);
    return reply;
  }
  const body = store.readAll(meta.id);
  reply.send(body);
  record(200, 'full', resolution.reason, null, meta, resolution, BigInt(body.length));
  return reply;
}

/** 400/416 拒绝（统一 JSON 错误包络；416 附 Content-Range: bytes 星号斜杠 size）。 */
function respondRejected(ctx: RequestContext, meta: ObjectMeta, resolution: RejectedResolution): FastifyReply {
  const { reply, requestId, isHead, record } = ctx;
  reply.code(resolution.statusCode);
  reply.header('Content-Type', 'application/json');
  if (resolution.statusCode === 416) {
    reply.header('Content-Range', `bytes */${meta.size.toString()}`);
  }
  const payloadBuf = Buffer.from(
    JSON.stringify(errorBody(resolution.reason, resolution.detail, requestId)),
    'utf8',
  );
  reply.header('Content-Length', payloadBuf.length.toString());
  if (isHead) {
    endHeadWithoutBody(reply);
  } else {
    reply.send(payloadBuf);
  }
  record(resolution.statusCode, 'rejected', resolution.reason, resolution.detail, meta, resolution, BigInt(payloadBuf.length));
  return reply;
}

/** 206 单区间。 */
function respondSinglePartial(ctx: RequestContext, meta: ObjectMeta, resolution: PartialResolution): FastifyReply {
  const { reply, store, isHead, record } = ctx;
  const iv = resolution.intervals[0]!;
  const length = iv.end - iv.start + 1n;
  reply.code(206);
  reply.header('Content-Type', meta.contentType);
  reply.header('Content-Range', `bytes ${iv.start.toString()}-${iv.end.toString()}/${meta.size.toString()}`);
  reply.header('Content-Length', length.toString());
  if (isHead) {
    endHeadWithoutBody(reply);
    record(206, 'partial', null, null, meta, resolution, length);
    return reply;
  }
  const chunk = store.readInterval(meta.id, iv.start, iv.end);
  reply.send(chunk);
  record(206, 'partial', null, null, meta, resolution, BigInt(chunk.length));
  return reply;
}

/** 206 multipart/byteranges：按 part 顺序拼装，并逐字节核对计划长度。 */
function respondMultipart(ctx: RequestContext, meta: ObjectMeta, resolution: PartialResolution): FastifyReply {
  const { req, reply, store, isHead, record } = ctx;
  const boundary = resolution.boundary!;
  const chunks: Buffer[] = [];
  for (const iv of resolution.intervals) {
    chunks.push(encodePartHeader(boundary, meta.contentType, iv, meta.size));
    chunks.push(store.readInterval(meta.id, iv.start, iv.end));
    chunks.push(encodePartTrailer());
  }
  chunks.push(encodeClosing(boundary));
  const body = Buffer.concat(chunks);
  const actualBodyBytes = BigInt(body.length);

  reply.code(206);
  reply.header('Content-Type', `multipart/byteranges; boundary=${boundary}`);
  reply.header('Content-Length', resolution.summary.totalBytes!.toString());

  if (actualBodyBytes !== resolution.summary.totalBytes) {
    // 计划长度与实际编码不一致属于服务端缺陷：记录日志（头已按计划发出）。
    req.log?.error?.(`multipart 计划长度不一致: plan=${resolution.summary.totalBytes} actual=${actualBodyBytes}`);
  }

  if (isHead) {
    endHeadWithoutBody(reply);
    record(206, 'partial', null, null, meta, resolution, resolution.summary.totalBytes);
    return reply;
  }
  reply.send(body);
  record(206, 'partial', null, null, meta, resolution, actualBodyBytes);
  return reply;
}

export function buildServer(options: BuildServerOptions): FastifyInstance {
  const app = Fastify({ logger: false });
  const decisionLog = new DecisionLog(options.diagnosticsCapacity);
  const policy = {
    maxRanges: options.maxRanges,
    maxResponseBytes: BigInt(options.maxResponseBytes),
  };

  /** 对象索引：方便首次使用者确认种子数据。 */
  app.get('/objects', async () => {
    const ids = options.store.listIds();
    return {
      success: true,
      data: ids.map((id) => {
        const meta = options.store.getMeta(id);
        return meta
          ? {
              id: meta.id,
              size: meta.size.toString(),
              etag: meta.etag,
              lastModified: formatImfFixdate(meta.lastModifiedMs),
              contentType: meta.contentType,
            }
          : { id };
      }),
    };
  });

  app.route<{ Params: { id: string } }>({
    method: ['GET', 'HEAD'],
    url: '/objects/:id',
    handler: async (req, reply) => {
      const rawRequestId = req.headers['x-request-id'];
      const requestId = (Array.isArray(rawRequestId) ? rawRequestId[0] : rawRequestId) ?? req.id ?? randomUUID();
      reply.header('X-Request-Id', requestId);
      const isHead = req.method === 'HEAD';

      const baseRecord = {
        requestId,
        timestamp: new Date().toISOString(),
        method: req.method,
        path: req.url,
        objectId: null as string | null,
        objectSize: null as string | null,
        requestHeaders: sanitizeHeaders(req.headers as Record<string, string | string[] | undefined>),
        multipart: null as boolean | null,
        intervals: [] as ReadonlyArray<{ start: string; end: string }>,
        actualBodyBytes: null as string | null,
        contentLengthHeader: null as string | null,
        headerBodyConsistent: null as boolean | null,
      };

      const record: RecordDecision = (statusCode, outcome, reason, detail, meta, resolution, entityBytes) => {
        const s = summaryToRecord(resolution?.summary ?? null);
        const intervals =
          resolution?.outcome === 'partial'
            ? resolution.intervals.map((iv) => ({ start: iv.start.toString(), end: iv.end.toString() }))
            : [];
        const contentLengthHeader = reply.getHeader('content-length');
        const contentLengthStr = contentLengthHeader === undefined ? null : String(contentLengthHeader);
        const headerBodyConsistent =
          contentLengthStr === null || entityBytes === null
            ? null
            : BigInt(contentLengthStr) === entityBytes;

        decisionLog.record({
          ...baseRecord,
          objectId: meta?.id ?? req.params.id,
          objectSize: meta ? meta.size.toString() : null,
          outcome,
          statusCode,
          reason,
          detail,
          multipart: resolution?.outcome === 'partial' ? resolution.multipart : null,
          intervals,
          parsedSpecCount: s.parsedSpecCount,
          satisfiableCount: s.satisfiableCount,
          unsatisfiableCount: s.unsatisfiableCount,
          unsatisfiableReasons: s.unsatisfiableReasons,
          plannedTotalBytes: s.plannedTotalBytes,
          actualBodyBytes: entityBytes === null ? null : entityBytes.toString(),
          contentLengthHeader: contentLengthStr,
          headerBodyConsistent,
        });
      };

      const meta = options.store.getMeta(req.params.id);
      if (!meta) {
        reply.code(404).header('content-type', 'application/json');
        reply.send(errorBody('object-not-found', `对象 ${req.params.id} 不存在`, requestId));
        record(404, 'not-found', 'object-not-found', null, null, null, null);
        return reply;
      }

      const rangeHeader = req.headers.range;
      const ifRangeHeader = req.headers['if-range'];
      const range = rangeHeader === undefined ? null : parseRangeHeader(String(rangeHeader));
      const ifRange = ifRangeHeader === undefined ? null : parseIfRange(String(ifRangeHeader));

      let resolution: Resolution;
      try {
        resolution = resolveRangeRequest({ meta, range, ifRange, policy, boundary: generateBoundary() });
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        reply.code(500).header('content-type', 'application/json');
        reply.send(errorBody('internal-error', message, requestId));
        record(500, 'error', 'kernel-threw', message, meta, null, null);
        return reply;
      }

      setRepresentationHeaders(reply, meta);
      const ctx: RequestContext = { req, reply, store: options.store, requestId, isHead, record };

      try {
        if (resolution.outcome === 'full') return respondFull(ctx, meta, resolution);
        if (resolution.outcome === 'rejected') return respondRejected(ctx, meta, resolution);
        return resolution.multipart
          ? respondMultipart(ctx, meta, resolution)
          : respondSinglePartial(ctx, meta, resolution);
      } catch (err) {
        // 存储读取等 I/O 失败：统一 500 包络并留诊断记录（可能已部分写出头，记录但不再改写响应）。
        const message = err instanceof Error ? err.message : String(err);
        req.log?.error?.(`对象读取失败 id=${meta.id}: ${message}`);
        if (!reply.sent && !reply.raw.headersSent) {
          reply.code(500).header('content-type', 'application/json');
          reply.send(errorBody('internal-error', message, requestId));
          record(500, 'error', 'store-read-failed', message, meta, resolution, null);
          return reply;
        }
        record(500, 'error', 'store-read-failed-after-headers', message, meta, resolution, null);
        return reply;
      }
    },
  });

  // —— 诊断接口 ——
  app.get('/_diagnostics/records', async (req: FastifyRequest, reply: FastifyReply) => {
    const query = req.query as { limit?: string };
    const limit = query.limit === undefined ? undefined : Number(query.limit);
    if (limit !== undefined && (!Number.isInteger(limit) || limit <= 0)) {
      reply.code(400);
      return errorBody('bad-limit', 'limit 必须是正整数', req.id ?? randomUUID());
    }
    return { success: true, data: decisionLog.list(limit === undefined ? {} : { limit }) };
  });

  app.route<{ Params: { requestId: string } }>({
    method: ['GET', 'HEAD'],
    url: '/_diagnostics/records/:requestId',
    handler: async (req, reply) => {
      const decisionRecord = decisionLog.get(req.params.requestId);
      if (!decisionRecord) {
        reply.code(404);
        return errorBody('record-not-found', `无请求 ${req.params.requestId} 的诊断记录`, req.id ?? randomUUID());
      }
      return { success: true, data: decisionRecord };
    },
  });

  return app;
}
