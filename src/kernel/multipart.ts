/**
 * multipart/byterants 封装（RFC 9110 14.6）。
 * 编码与“长度计算”成对存在且必须逐字节一致；测试会用实际编码长度反校。
 */
import { randomBytes } from 'node:crypto';
import type { ObjectMeta, ResolvedInterval } from './types.js';

const CRLF = '\r\n';

export interface MultipartLayout {
  readonly boundary: string;
  readonly contentType: string;
  /** 每个 part 的封装头长度（含前导 boundary 行与头后空行）。 */
  readonly perPartHeaderSizes: readonly bigint[];
  /** 每个 part 数据后的 CRLF 长度。 */
  readonly perPartTrailerSize: bigint;
  /** 结束 boundary 行长度：--boundary--\r\n。 */
  readonly closingSize: bigint;
  readonly overheadBytes: bigint;
}

function partHeader(boundary: string, contentType: string, iv: ResolvedInterval, size: bigint): string {
  return (
    `--${boundary}${CRLF}` +
    `Content-Type: ${contentType}${CRLF}` +
    `Content-Range: bytes ${iv.start.toString()}-${iv.end.toString()}/${size.toString()}${CRLF}` +
    CRLF
  );
}

/** 纯长度计算，供执行内核在读取任何字节之前判断总量是否超限。 */
export function planMultipart(
  boundary: string,
  meta: ObjectMeta,
  intervals: readonly ResolvedInterval[],
): MultipartLayout {
  const headerSizes = intervals.map((iv) =>
    BigInt(Buffer.byteLength(partHeader(boundary, meta.contentType, iv, meta.size), 'ascii')),
  );
  const trailer = BigInt(Buffer.byteLength(CRLF, 'ascii'));
  const closing = BigInt(Buffer.byteLength(`--${boundary}--${CRLF}`, 'ascii'));
  const overhead =
    headerSizes.reduce((acc, n) => acc + n, 0n) + trailer * BigInt(intervals.length) + closing;
  return {
    boundary,
    contentType: `multipart/byteranges; boundary=${boundary}`,
    perPartHeaderSizes: headerSizes,
    perPartTrailerSize: trailer,
    closingSize: closing,
    overheadBytes: overhead,
  };
}

/** 生成某个 part 之前的封装头字节。 */
export function encodePartHeader(
  boundary: string,
  contentType: string,
  iv: ResolvedInterval,
  size: bigint,
): Buffer {
  return Buffer.from(partHeader(boundary, contentType, iv, size), 'ascii');
}

export function encodePartTrailer(): Buffer {
  return Buffer.from(CRLF, 'ascii');
}

export function encodeClosing(boundary: string): Buffer {
  return Buffer.from(`--${boundary}--${CRLF}`, 'ascii');
}

/** 生成不含 "--" 前缀、只使用安全字符的 boundary。 */
export function generateBoundary(byteCount = 16): string {
  const token = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ';
  const bytes = randomBytes(byteCount);
  let out = 'rrbb';
  for (const b of bytes) out += token[b % token.length] ?? '0';
  return out;
}
