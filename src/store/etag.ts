import { createHash } from 'node:crypto';

/**
 * 强验证子（RFC 9110 8.8.1）：对原始表示字节做 SHA-256。
 * 对象不可变，ETag 可长期缓存；含引号的完整形式可直接进头。
 */
export function strongEtag(content: Buffer): string {
  return `"${createHash('sha256').update(content).digest('hex')}"`;
}
