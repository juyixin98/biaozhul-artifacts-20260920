/**
 * 本地合成夹具：所有样例对象确定性生成，无外部账号/真实业务数据。
 * 种子脚本与集成测试共用同一份定义，保证“看到的”和“测到的”一致。
 */
export interface SampleObject {
  readonly id: string;
  readonly contentType: string;
  /** 固定的 Last-Modified 时间（UTC 毫秒），便于 If-Range 日期断言。 */
  readonly lastModifiedMs: number;
  readonly content: Buffer;
  readonly description: string;
}

/** 2026-01-02T03:04:05Z —— 固定时刻，断言可复现。 */
export const FIXED_MODIFIED_MS = Date.UTC(2026, 0, 2, 3, 4, 5);

function ascii(text: string): Buffer {
  return Buffer.from(text, 'ascii');
}

function repeating(pattern: string, totalLength: number): Buffer {
  return Buffer.alloc(totalLength, pattern, 'ascii');
}

/** 0..255 两轮的二进制样例，便于逐字节核验偏移。 */
function binaryBytes512(): Buffer {
  const buf = Buffer.alloc(512);
  for (let i = 0; i < 512; i++) buf[i] = i % 256;
  return buf;
}

export const SAMPLE_OBJECTS: readonly SampleObject[] = [
  {
    id: 'hello-txt',
    contentType: 'text/plain; charset=utf-8',
    lastModifiedMs: FIXED_MODIFIED_MS,
    content: ascii('Hello, Range world!'), // 19 字节
    description: '小文本对象，19 字节',
  },
  {
    id: 'alphabet',
    contentType: 'text/plain; charset=ascii',
    lastModifiedMs: FIXED_MODIFIED_MS,
    content: ascii('ABCDEFGHIJKLMNOPQRSTUVWXYZ'), // 26 字节
    description: 'A-Z 字母表，26 字节，后缀/越界测试用',
  },
  {
    id: 'zero-empty',
    contentType: 'application/octet-stream',
    lastModifiedMs: FIXED_MODIFIED_MS,
    content: Buffer.alloc(0),
    description: '零长度对象，416 与空 200 测试用',
  },
  {
    id: 'binary-512',
    contentType: 'application/octet-stream',
    lastModifiedMs: FIXED_MODIFIED_MS,
    content: binaryBytes512(),
    description: '0..255 重复两轮的 512 字节二进制对象',
  },
  {
    id: 'padding-10k',
    contentType: 'text/plain; charset=ascii',
    lastModifiedMs: FIXED_MODIFIED_MS,
    content: repeating('abcdefghij', 10_000),
    description: '10000 字节重复模式，总量限额/大偏移测试用',
  },
];
