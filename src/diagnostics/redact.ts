/**
 * 脱敏工具。诊断记录永远不得包含对象内容；任何可能携带凭证的头
 * （Authorization / Cookie / X-Api-Key 等）只保留掩码。
 */

const SENSITIVE_HEADER_NAMES = new Set([
  'authorization',
  'cookie',
  'set-cookie',
  'proxy-authorization',
  'x-api-key',
  'x-auth-token',
]);

/** 保留首尾少量字符，中间以 *** 代替；短串整体掩码。 */
export function redactSecret(value: string): string {
  if (value.length <= 8) return '***';
  return `${value.slice(0, 3)}***${value.slice(-2)}`;
}

/**
 * 从请求头中挑出与 Range 判定相关的白名单头；敏感头一律脱敏。
 * 头值统一转字符串（Node 下 string | string[] | undefined）。
 */
export function sanitizeHeaders(
  headers: Record<string, string | string[] | undefined>,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [name, raw] of Object.entries(headers)) {
    if (raw === undefined) continue;
    const value = Array.isArray(raw) ? raw.join(', ') : raw;
    out[name.toLowerCase()] = SENSITIVE_HEADER_NAMES.has(name.toLowerCase())
      ? redactSecret(value)
      : value;
  }
  return out;
}
