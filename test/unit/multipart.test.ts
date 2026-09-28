import { describe, expect, it } from 'vitest';
import {
  encodeClosing,
  encodePartHeader,
  encodePartTrailer,
  planMultipart,
} from '../../src/kernel/multipart.js';
import type { ObjectMeta, ResolvedInterval } from '../../src/kernel/types.js';

const META: ObjectMeta = {
  id: 'x',
  size: 26n,
  etag: '"e"',
  lastModifiedMs: 0,
  contentType: 'text/plain',
};

const IVS: readonly ResolvedInterval[] = [
  { start: 0n, end: 2n },
  { start: 10n, end: 12n },
];

/**
 * 独立参考解析器：不调用任何被测编码函数，直接按 RFC 9110 14.6 的线格式
 * 手工切包，用于核验被测方实际发出的字节。
 */
function parseMultipartBody(body: Buffer, boundary: string) {
  const text = body.toString('latin1');
  const parts: Array<{ headers: Record<string, string>; data: Buffer; dataRange: [number, number] }> = [];
  const marker = `--${boundary}`;
  let cursor = 0;

  while (true) {
    const start = text.indexOf(marker, cursor);
    if (start < 0) throw new Error(`找不到 boundary: ${marker}`);
    const afterMarker = start + marker.length;
    if (text.startsWith('--', afterMarker)) {
      // 结束标记
      const closingEnd = text.indexOf('\r\n', afterMarker);
      return { parts, closingConsumed: (closingEnd < 0 ? text.length : closingEnd + 2) - start };
    }
    const headerStart = afterMarker + 2; // 跳过 CRLF
    const headerEnd = text.indexOf('\r\n\r\n', headerStart);
    if (headerEnd < 0) throw new Error('找不到 part 头结束空行');
    const headerBlock = text.slice(headerStart, headerEnd);
    const headers: Record<string, string> = {};
    for (const line of headerBlock.split('\r\n')) {
      const colon = line.indexOf(':');
      if (colon >= 0) headers[line.slice(0, colon).trim().toLowerCase()] = line.slice(colon + 1).trim();
    }
    const dataStart = headerEnd + 4;
    const nextMarker = text.indexOf(`\r\n${marker}`, dataStart);
    if (nextMarker < 0) throw new Error('找不到下一个 boundary');
    const data = body.subarray(dataStart, nextMarker);
    const cr = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(headers['content-range'] ?? '');
    if (!cr) throw new Error(`Content-Range 非法: ${headers['content-range']}`);
    parts.push({
      headers,
      data,
      dataRange: [Number(cr[1]), Number(cr[2])],
    });
    cursor = nextMarker + 2; // 跳过 CRLF，下一轮从 marker 开始
  }
}

describe('multipart 编码 —— 计划长度与实际字节逐字节一致', () => {
  const boundary = 'BOUND';
  const alphabet = Buffer.from('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'ascii');

  it('planMultipart 算出的开销等于实际封装字节', () => {
    const plan = planMultipart(boundary, META, IVS);
    const chunks: Buffer[] = [];
    for (const iv of IVS) {
      chunks.push(encodePartHeader(boundary, META.contentType, iv, META.size));
      chunks.push(alphabet.subarray(Number(iv.start), Number(iv.end) + 1));
      chunks.push(encodePartTrailer());
    }
    chunks.push(encodeClosing(boundary));
    const body = Buffer.concat(chunks);

    const dataBytes = IVS.reduce((acc, iv) => acc + Number(iv.end - iv.start + 1n), 0);
    expect(Number(plan.overheadBytes)).toBe(body.length - dataBytes);
    expect(body.length).toBe(dataBytes + Number(plan.overheadBytes));
  });

  it('参考解析器能拆出每个 part，且数据切片与原对象逐字节相同', () => {
    const chunks: Buffer[] = [];
    for (const iv of IVS) {
      chunks.push(encodePartHeader(boundary, META.contentType, iv, META.size));
      chunks.push(alphabet.subarray(Number(iv.start), Number(iv.end) + 1));
      chunks.push(encodePartTrailer());
    }
    chunks.push(encodeClosing(boundary));
    const body = Buffer.concat(chunks);

    const { parts } = parseMultipartBody(body, boundary);
    expect(parts).toHaveLength(2);
    expect(parts[0]!.headers['content-type']).toBe('text/plain');
    expect(parts[0]!.dataRange).toEqual([0, 2]);
    expect(parts[0]!.data.toString('ascii')).toBe('ABC');
    expect(parts[1]!.data.toString('ascii')).toBe('KLM');
    expect(parts[1]!.headers['content-range']).toBe('bytes 10-12/26');
  });

  it('单 part 也可被参考解析器正确处理，结束符为 --boundary--', () => {
    const iv = [{ start: 25n, end: 25n }];
    const body = Buffer.concat([
      encodePartHeader(boundary, META.contentType, iv[0]!, META.size),
      alphabet.subarray(25, 26),
      encodePartTrailer(),
      encodeClosing(boundary),
    ]);
    const { parts } = parseMultipartBody(body, boundary);
    expect(parts).toHaveLength(1);
    expect(parts[0]!.data.toString('ascii')).toBe('Z');
    expect(body.subarray(body.length - `--${boundary}--\r\n`.length).toString('ascii'))
      .toBe(`--${boundary}--\r\n`);
  });
});
