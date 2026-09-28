import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { FastifyInstance } from 'fastify';
import { SAMPLE_OBJECTS } from '../../fixtures/samples.js';
import { buildServer } from '../../src/server.js';
import { strongEtag } from '../../src/store/etag.js';
import { SqliteObjectStore } from '../../src/store/sqliteStore.js';

/**
 * 集成测试：真实 HTTP 层 + 真实 SQLite（内存模式）。
 * 参考字节一律来自 fixtures 中的原始内容（测试数据），
 * multipart 用独立解析器重组——不调用任何被测编码函数。
 */

const LIMITS = { maxRanges: 4, maxResponseBytes: 64 * 1024 };

let store: SqliteObjectStore;
let app: FastifyInstance;

function sample(id: string) {
  const s = SAMPLE_OBJECTS.find((o) => o.id === id);
  if (!s) throw new Error(`未知样例 ${id}`);
  return s;
}

beforeAll(async () => {
  store = new SqliteObjectStore(':memory:');
  for (const s of SAMPLE_OBJECTS) {
    store.putObject({
      id: s.id,
      content: s.content,
      contentType: s.contentType,
      lastModifiedMs: s.lastModifiedMs,
      etag: strongEtag(s.content),
    });
  }
  app = buildServer({
    store,
    maxRanges: LIMITS.maxRanges,
    maxResponseBytes: LIMITS.maxResponseBytes,
    diagnosticsCapacity: 100,
  });
  await app.ready();
});

afterAll(async () => {
  await app.close();
  store.close();
});

/** 独立 multipart 解析器（游标扫描，不复用任何被测编码函数）。 */
function parseMultipart(body: Buffer, contentType: string) {
  const m = /boundary=([^\s;]+)/.exec(contentType);
  if (!m) throw new Error(`Content-Type 缺少 boundary: ${contentType}`);
  const boundary = m[1]!;
  const marker = Buffer.from(`--${boundary}`, 'ascii');
  const parts: Array<{ contentRange: string; data: Buffer }> = [];
  let cursor = body.indexOf(marker);
  expect(cursor).toBe(0);
  cursor += marker.length;

  while (true) {
    if (body.subarray(cursor, cursor + 2).toString('ascii') === '--') {
      // 结束标记 --boundary--\r\n
      expect(body.subarray(cursor + 2, cursor + 4).toString('ascii')).toBe('\r\n');
      return { boundary, parts };
    }
    // 每个 part：CRLF + 头 + CRLF CRLF + 数据 + CRLF
    expect(body.subarray(cursor, cursor + 2).toString('ascii')).toBe('\r\n');
    const sep = Buffer.from('\r\n\r\n', 'ascii');
    const headerStart = cursor + 2;
    const headerEnd = body.indexOf(sep, headerStart);
    expect(headerEnd).toBeGreaterThan(0);
    const headerBlock = body.subarray(headerStart, headerEnd).toString('ascii');
    const cr = /Content-Range: (bytes \d+-\d+\/\d+)/i.exec(headerBlock);
    expect(cr, `part 头缺少 Content-Range: ${headerBlock}`).not.toBeNull();
    const dataStart = headerEnd + 4;
    const nextMarker = body.indexOf(Buffer.concat([Buffer.from('\r\n', 'ascii'), marker]), dataStart);
    expect(nextMarker).toBeGreaterThan(0);
    parts.push({ contentRange: cr![1]!, data: body.subarray(dataStart, nextMarker) });
    cursor = nextMarker + 2 + marker.length; // 跳过 CRLF 与 marker
  }
}

describe('完整表示与基础头', () => {
  it('GET 无 Range → 200，体与夹具逐字节一致，头体长度一致', async () => {
    const s = sample('hello-txt');
    const res = await app.inject({ method: 'GET', url: `/objects/${s.id}` });
    expect(res.statusCode).toBe(200);
    expect(res.headers['content-type']).toBe(s.contentType);
    expect(res.headers['accept-ranges']).toBe('bytes');
    // 期望值为独立计算并在种子输出中固定下来的 SHA-256，而非调用被测 strongEtag 生成。
    expect(res.headers['etag']).toBe(
      '"5784ba23c4bbf96fa82f732fa4612c32c528c61812da7d69c9cc0a6290c010a0"',
    );
    expect(res.headers['last-modified']).toBe('Fri, 02 Jan 2026 03:04:05 GMT');
    expect(res.rawPayload.equals(s.content)).toBe(true);
    expect(Number(res.headers['content-length'])).toBe(res.rawPayload.length);
    expect(res.headers['x-request-id']).toBeTruthy();
  });

  it('HEAD → 与 GET 相同的实体头，无体', async () => {
    const s = sample('binary-512');
    const res = await app.inject({ method: 'HEAD', url: `/objects/${s.id}` });
    expect(res.statusCode).toBe(200);
    expect(res.headers['content-length']).toBe(String(s.content.length));
    expect(res.rawPayload.length).toBe(0);
  });

  it('未知对象 → 404 统一错误包络', async () => {
    const res = await app.inject({ method: 'GET', url: '/objects/nope' });
    expect(res.statusCode).toBe(404);
    const body = res.json();
    expect(body.success).toBe(false);
    expect(body.error.code).toBe('object-not-found');
    expect(body.error.requestId).toBeTruthy();
  });

  it('零长度对象 GET → 200 空体 Content-Length: 0', async () => {
    const res = await app.inject({ method: 'GET', url: '/objects/zero-empty' });
    expect(res.statusCode).toBe(200);
    expect(res.headers['content-length']).toBe('0');
    expect(res.rawPayload.length).toBe(0);
  });
});

describe('单范围 206', () => {
  it('显式区间 → 体为原对象对应切片（逐字节）', async () => {
    const s = sample('alphabet');
    const res = await app.inject({
      method: 'GET',
      url: `/objects/${s.id}`,
      headers: { range: 'bytes=3-7' },
    });
    expect(res.statusCode).toBe(206);
    expect(res.headers['content-range']).toBe('bytes 3-7/26');
    expect(res.headers['content-length']).toBe('5');
    expect(res.rawPayload.equals(s.content.subarray(3, 8))).toBe(true);
    expect(res.rawPayload.toString('ascii')).toBe('DEFGH');
    expect(Number(res.headers['content-length'])).toBe(res.rawPayload.length);
  });

  it('开放结尾 bytes=20- → 到对象末尾', async () => {
    const s = sample('alphabet');
    const res = await app.inject({
      method: 'GET',
      url: `/objects/${s.id}`,
      headers: { range: 'bytes=20-' },
    });
    expect(res.statusCode).toBe(206);
    expect(res.headers['content-range']).toBe('bytes 20-25/26');
    expect(res.rawPayload.toString('ascii')).toBe('UVWXYZ');
  });

  it('后缀 bytes=-4 → 末尾 4 字节', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/objects/alphabet',
      headers: { range: 'bytes=-4' },
    });
    expect(res.statusCode).toBe(206);
    expect(res.headers['content-range']).toBe('bytes 22-25/26');
    expect(res.rawPayload.toString('ascii')).toBe('WXYZ');
  });

  it('结束越界被裁剪 bytes=24-999 → 206 末两字节', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/objects/alphabet',
      headers: { range: 'bytes=24-999' },
    });
    expect(res.statusCode).toBe(206);
    expect(res.headers['content-range']).toBe('bytes 24-25/26');
    expect(res.rawPayload.toString('ascii')).toBe('YZ');
  });

  it('二进制对象中间切片逐字节一致（偏移基于原对象）', async () => {
    const s = sample('binary-512');
    const res = await app.inject({
      method: 'GET',
      url: `/objects/${s.id}`,
      headers: { range: 'bytes=250-260' },
    });
    expect(res.statusCode).toBe(206);
    expect(res.rawPayload.equals(s.content.subarray(250, 261))).toBe(true);
    // 独立参考：内容定义为 i % 256
    for (let i = 0; i < res.rawPayload.length; i++) {
      expect(res.rawPayload[i]).toBe((250 + i) % 256);
    }
  });
});

describe('416 / 400 拒绝类别', () => {
  it('起始越界 → 416 + Content-Range: bytes */size', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/objects/alphabet',
      headers: { range: 'bytes=26-30' },
    });
    expect(res.statusCode).toBe(416);
    expect(res.headers['content-range']).toBe('bytes */26');
    expect(res.json().error.code).toBe('none-satisfiable');
  });

  it('极大整数起始（>2^53）→ 416 而非崩溃或精度错乱', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/objects/alphabet',
      headers: { range: 'bytes=9007199254740993-9007199254741000' },
    });
    expect(res.statusCode).toBe(416);
    expect(res.headers['content-range']).toBe('bytes */26');
  });

  it('极大整数结束被裁剪 → 206 到对象末尾', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/objects/alphabet',
      headers: { range: 'bytes=25-99999999999999999999' },
    });
    expect(res.statusCode).toBe(206);
    expect(res.headers['content-range']).toBe('bytes 25-25/26');
    expect(res.rawPayload.toString('ascii')).toBe('Z');
  });

  it('零长度对象上的范围 → 416，Content-Range: bytes */0', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/objects/zero-empty',
      headers: { range: 'bytes=0-0' },
    });
    expect(res.statusCode).toBe(416);
    expect(res.headers['content-range']).toBe('bytes */0');
  });

  it('语法损坏 → 400 malformed-range', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/objects/alphabet',
      headers: { range: 'bytes=abc-def' },
    });
    expect(res.statusCode).toBe(400);
    expect(res.json().error.code).toBe('malformed-range');
  });

  it('未知 range-unit → 忽略 Range 回 200 完整表示', async () => {
    const s = sample('alphabet');
    const res = await app.inject({
      method: 'GET',
      url: `/objects/${s.id}`,
      headers: { range: 'items=0-3' },
    });
    expect(res.statusCode).toBe(200);
    expect(res.rawPayload.equals(s.content)).toBe(true);
  });

  it('范围数量超限 → 400 too-many-ranges', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/objects/alphabet',
      headers: { range: 'bytes=0-1,2-3,4-5,6-7,8-9' }, // 5 > maxRanges=4
    });
    expect(res.statusCode).toBe(400);
    expect(res.json().error.code).toBe('too-many-ranges');
  });
});

describe('多范围合并与 multipart', () => {
  it('相邻范围合并为单区间 → 206 非 multipart', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/objects/alphabet',
      headers: { range: 'bytes=0-4,5-9' },
    });
    expect(res.statusCode).toBe(206);
    expect(res.headers['content-type']).not.toContain('multipart');
    expect(res.headers['content-range']).toBe('bytes 0-9/26');
    expect(res.rawPayload.toString('ascii')).toBe('ABCDEFGHIJ');
  });

  it('边界重叠范围合并（0-9 与 9-19 共享字节 9）', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/objects/alphabet',
      headers: { range: 'bytes=0-9,9-19' },
    });
    expect(res.statusCode).toBe(206);
    expect(res.headers['content-range']).toBe('bytes 0-19/26');
    expect(res.rawPayload.toString('ascii')).toBe('ABCDEFGHIJKLMNOPQRST');
  });

  it('不相邻两范围 → multipart，重组后逐字节核验，头体长度一致', async () => {
    const s = sample('alphabet');
    const res = await app.inject({
      method: 'GET',
      url: `/objects/${s.id}`,
      headers: { range: 'bytes=0-2,23-25' },
    });
    expect(res.statusCode).toBe(206);
    expect(res.headers['content-type']).toContain('multipart/byteranges');
    // 头体长度一致
    expect(Number(res.headers['content-length'])).toBe(res.rawPayload.length);

    const { parts } = parseMultipart(res.rawPayload, String(res.headers['content-type']));
    expect(parts).toHaveLength(2);
    expect(parts[0]!.contentRange).toBe('bytes 0-2/26');
    expect(parts[0]!.data.toString('ascii')).toBe('ABC');
    expect(parts[1]!.contentRange).toBe('bytes 23-25/26');
    expect(parts[1]!.data.toString('ascii')).toBe('XYZ');
    // 重组：按 Content-Range 声明的位置写回 26 字节缓冲；
    // 仅被覆盖位置必须与原对象一致，未请求位置保持填充（不臆造数据）。
    const restored = Buffer.alloc(26, 0x2e); // '.' 填充
    const covered = new Array(26).fill(false);
    for (const p of parts) {
      const m = /bytes (\d+)-(\d+)\/26/.exec(p.contentRange)!;
      const start = Number(m[1]);
      const end = Number(m[2]);
      p.data.copy(restored, start);
      expect(p.data.length).toBe(end - start + 1);
      for (let i = start; i <= end; i++) covered[i] = true;
    }
    for (let i = 0; i < 26; i++) {
      expect(restored[i], `位置 ${i}`).toBe(covered[i] ? s.content[i] : 0x2e);
    }
  });

  it('部分合法（一合法一越界）→ 206 只回合法区间', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/objects/alphabet',
      headers: { range: 'bytes=0-2,100-200' },
    });
    expect(res.statusCode).toBe(206);
    expect(res.headers['content-range']).toBe('bytes 0-2/26');
    expect(res.rawPayload.toString('ascii')).toBe('ABC');
  });

  it('三范围乱序 → 排序合并后 multipart 顺序正确', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/objects/alphabet',
      headers: { range: 'bytes=20-21,0-1,10-11' },
    });
    expect(res.statusCode).toBe(206);
    const { parts } = parseMultipart(res.rawPayload, String(res.headers['content-type']));
    expect(parts.map((p) => p.contentRange)).toEqual([
      'bytes 0-1/26',
      'bytes 10-11/26',
      'bytes 20-21/26',
    ]);
    expect(parts.map((p) => p.data.toString('ascii'))).toEqual(['AB', 'KL', 'UV']);
  });
});

describe('If-Range', () => {
  it('ETag 匹配 → 206', async () => {
    const s = sample('alphabet');
    const res = await app.inject({
      method: 'GET',
      url: `/objects/${s.id}`,
      headers: { range: 'bytes=0-2', 'if-range': strongEtag(s.content) },
    });
    expect(res.statusCode).toBe(206);
    expect(res.rawPayload.toString('ascii')).toBe('ABC');
  });

  it('ETag 不匹配 → 200 完整表示（不是 412/416）', async () => {
    const s = sample('alphabet');
    const res = await app.inject({
      method: 'GET',
      url: `/objects/${s.id}`,
      headers: { range: 'bytes=0-2', 'if-range': '"stale-etag"' },
    });
    expect(res.statusCode).toBe(200);
    expect(res.rawPayload.equals(s.content)).toBe(true);
  });

  it('日期晚于 Last-Modified → 206；早于 → 200', async () => {
    const s = sample('alphabet');
    const fresh = await app.inject({
      method: 'GET',
      url: `/objects/${s.id}`,
      headers: { range: 'bytes=0-2', 'if-range': 'Sat, 03 Jan 2026 00:00:00 GMT' },
    });
    expect(fresh.statusCode).toBe(206);

    const stale = await app.inject({
      method: 'GET',
      url: `/objects/${s.id}`,
      headers: { range: 'bytes=0-2', 'if-range': 'Thu, 01 Jan 2026 00:00:00 GMT' },
    });
    expect(stale.statusCode).toBe(200);
    expect(stale.rawPayload.equals(s.content)).toBe(true);
  });

  it('弱 ETag → 无法判定 → 200 完整表示', async () => {
    const s = sample('alphabet');
    const res = await app.inject({
      method: 'GET',
      url: `/objects/${s.id}`,
      headers: { range: 'bytes=0-2', 'if-range': `W/${strongEtag(s.content)}` },
    });
    expect(res.statusCode).toBe(200);
    expect(res.rawPayload.equals(s.content)).toBe(true);
  });
});

describe('诊断接口', () => {
  it('记录带请求标识、结果、原因与关键状态；X-Request-Id 可回查', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/objects/alphabet',
      headers: { range: 'bytes=0-2,100-200', 'x-request-id': 'req-it-001' },
    });
    expect(res.statusCode).toBe(206);
    expect(res.headers['x-request-id']).toBe('req-it-001');

    const diag = await app.inject({ method: 'GET', url: '/_diagnostics/records/req-it-001' });
    expect(diag.statusCode).toBe(200);
    const rec = diag.json().data;
    expect(rec.requestId).toBe('req-it-001');
    expect(rec.outcome).toBe('partial');
    expect(rec.statusCode).toBe(206);
    expect(rec.objectSize).toBe('26');
    expect(rec.parsedSpecCount).toBe(2);
    expect(rec.satisfiableCount).toBe(1);
    expect(rec.unsatisfiableCount).toBe(1);
    expect(rec.unsatisfiableReasons).toEqual(['start-beyond-size']);
    expect(rec.headerBodyConsistent).toBe(true);
    expect(rec.actualBodyBytes).toBe('3');
  });

  it('416 拒绝也有记录且原因可读', async () => {
    await app.inject({
      method: 'GET',
      url: '/objects/alphabet',
      headers: { range: 'bytes=999-', 'x-request-id': 'req-it-416' },
    });
    const diag = await app.inject({ method: 'GET', url: '/_diagnostics/records/req-it-416' });
    const rec = diag.json().data;
    expect(rec.outcome).toBe('rejected');
    expect(rec.statusCode).toBe(416);
    expect(rec.reason).toBe('none-satisfiable');
    expect(rec.detail).toContain('26');
  });

  it('敏感请求头在记录中被脱敏', async () => {
    await app.inject({
      method: 'GET',
      url: '/objects/alphabet',
      headers: {
        range: 'bytes=0-1',
        authorization: 'Bearer top-secret-token-value',
        'x-request-id': 'req-it-secret',
      },
    });
    const diag = await app.inject({ method: 'GET', url: '/_diagnostics/records/req-it-secret' });
    const rec = diag.json().data;
    const serialized = JSON.stringify(rec);
    expect(serialized).not.toContain('top-secret-token-value');
    expect(rec.requestHeaders['authorization']).toContain('***');
    expect(rec.requestHeaders['range']).toBe('bytes=0-1');
  });

  it('列表接口按时间倒序返回，limit 生效', async () => {
    const res = await app.inject({ method: 'GET', url: '/_diagnostics/records?limit=3' });
    expect(res.statusCode).toBe(200);
    const data = res.json().data;
    expect(data.length).toBeLessThanOrEqual(3);
    expect(data[0].requestId).toBe('req-it-secret'); // 最近一条
  });

  it('未知 requestId → 404', async () => {
    const res = await app.inject({ method: 'GET', url: '/_diagnostics/records/no-such' });
    expect(res.statusCode).toBe(404);
  });

  it('limit 非正整数 → 400 bad-limit', async () => {
    const bad = await app.inject({ method: 'GET', url: '/_diagnostics/records?limit=0' });
    expect(bad.statusCode).toBe(400);
    expect(bad.json().error.code).toBe('bad-limit');
    const nan = await app.inject({ method: 'GET', url: '/_diagnostics/records?limit=abc' });
    expect(nan.statusCode).toBe(400);
  });
});

describe('对象清单', () => {
  it('GET /objects 返回全部样例的元数据（size 为字符串化 bigint）', async () => {
    const res = await app.inject({ method: 'GET', url: '/objects' });
    expect(res.statusCode).toBe(200);
    const items = res.json().data as Array<{
      id: string;
      size: string;
      contentType: string;
      lastModified: string;
    }>;
    const byId = new Map(items.map((i) => [i.id, i]));
    expect(byId.get('alphabet')).toMatchObject({
      id: 'alphabet',
      size: '26',
      contentType: 'text/plain; charset=ascii',
      lastModified: 'Fri, 02 Jan 2026 03:04:05 GMT',
    });
    expect(byId.get('zero-empty')!.size).toBe('0');
    expect(byId.get('padding-10k')!.size).toBe('10000');
  });
});

describe('限额：总响应量', () => {
  it('响应总量超限 → 400 response-too-large', async () => {
    const tightApp = buildServer({
      store,
      maxRanges: 8,
      maxResponseBytes: 100, // 远小于 padding-10k
      diagnosticsCapacity: 10,
    });
    await tightApp.ready();
    const res = await tightApp.inject({
      method: 'GET',
      url: '/objects/padding-10k',
      headers: { range: 'bytes=0-4999' },
    });
    expect(res.statusCode).toBe(400);
    expect(res.json().error.code).toBe('response-too-large');
    await tightApp.close();
  });

  it('未超限的大对象范围正常返回且逐字节一致', async () => {
    const s = sample('padding-10k');
    const res = await app.inject({
      method: 'GET',
      url: `/objects/${s.id}`,
      headers: { range: 'bytes=9990-9999' },
    });
    expect(res.statusCode).toBe(206);
    expect(res.headers['content-range']).toBe('bytes 9990-9999/10000');
    expect(res.rawPayload.equals(s.content.subarray(9990, 10000))).toBe(true);
  });

  it('存储读取异常 → 500 统一包络且诊断原因是 store-read-failed', async () => {
    class FailingStore extends SqliteObjectStore {
      override readAll(): Buffer {
        throw new Error('simulated disk failure');
      }
      override readInterval(): Buffer {
        throw new Error('simulated disk failure');
      }
    }
    const failStore = new FailingStore(':memory:');
    failStore.putObject({
      id: 'doomed',
      content: Buffer.from('0123456789'),
      contentType: 'text/plain',
      lastModifiedMs: 0,
      etag: '"d"',
    });
    const failApp = buildServer({
      store: failStore,
      maxRanges: 4,
      maxResponseBytes: 64 * 1024,
      diagnosticsCapacity: 10,
    });
    await failApp.ready();

    const res = await failApp.inject({
      method: 'GET',
      url: '/objects/doomed',
      headers: { 'x-request-id': 'req-it-iofail' },
    });
    expect(res.statusCode).toBe(500);
    expect(res.json().error.code).toBe('internal-error');
    const diag = await failApp.inject({ method: 'GET', url: '/_diagnostics/records/req-it-iofail' });
    expect(diag.json().data.reason).toBe('store-read-failed');
    await failApp.close();
    failStore.close();
  });
});
