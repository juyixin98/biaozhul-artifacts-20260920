import type { JsonValue } from '../contract/protocol.js';

/**
 * Redaction for diagnostics. Business data stays in its normal tables; this
 * only guards what gets written into diagnostic event records and log lines.
 */

const SENSITIVE_WORDS = new Set([
  'password',
  'passwd',
  'pwd',
  'secret',
  'token',
  'authorization',
  'authentication',
  'auth',
  'apikey',
  'credential',
  'jwt',
]);

/** Multi-word phrases that are sensitive even though each token alone is not. */
const SENSITIVE_PHRASES = new Set(['api key', 'private key', 'secret key', 'access key']);

const REDACTED = '***REDACTED***';
const MAX_STRING_LENGTH = 500;

/**
 * Decide whether a key name is sensitive. The key is normalized first so
 * camelCase / kebab / snake forms all reduce to the same tokens:
 *   passwordHash -> ["password","hash"]      (catches prefix-style names)
 *   accessToken  -> ["access","token"]
 *   X-API-KEY    -> ["x","api","key"]
 *   client_secret-> ["client","secret"]
 * A match is any SENSITIVE word as a whole token, or the bigrams
 * "api key". Trailing plural "s" is tolerated (tokens/secrets/passwords).
 */
function keyIsSensitive(key: string): boolean {
  const normalized = key
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_\-\s.]+/g, ' ')
    .toLowerCase()
    .trim();

  if (SENSITIVE_PHRASES.has(normalized)) return true;
  for (const phrase of SENSITIVE_PHRASES) {
    if (normalized.includes(` ${phrase} `) || normalized.startsWith(`${phrase} `) || normalized.endsWith(` ${phrase}`)) {
      return true;
    }
  }

  return normalized.split(' ').some((token) => {
    const singular = token.endsWith('s') && token.length > 1 ? token.slice(0, -1) : token;
    return SENSITIVE_WORDS.has(token) || SENSITIVE_WORDS.has(singular);
  });
}

export function redact(value: unknown): JsonValue | undefined {
  if (value === undefined) return undefined;
  return redactValue(value, new WeakMap());
}

function redactValue(value: unknown, seen: WeakMap<object, unknown>): JsonValue {
  if (value === null) return null;
  const t = typeof value;
  if (t === 'string') return redactString(value as string);
  if (t === 'number' || t === 'boolean') return value as JsonValue;
  if (Array.isArray(value)) {
    if (seen.has(value)) return '[Circular]';
    seen.set(value, null);
    return value.map((item) => redactValue(item, seen));
  }
  if (t === 'object') {
    const obj = value as Record<string, unknown>;
    if (seen.has(obj)) return '[Circular]';
    seen.set(obj, null);
    const out: Record<string, JsonValue> = {};
    for (const [key, v] of Object.entries(obj)) {
      if (keyIsSensitive(key)) {
        out[key] = REDACTED;
      } else {
        out[key] = redactValue(v, seen);
      }
    }
    return out;
  }
  // bigint, function, symbol: never valid JSON anyway.
  return String(value);
}

function redactString(s: string): string {
  if (s.length <= MAX_STRING_LENGTH) return s;
  return `${s.slice(0, MAX_STRING_LENGTH)}…<${s.length - MAX_STRING_LENGTH} more chars>`;
}

/** True when a params object carries one of the sensitive key names. */
export function containsSensitiveKey(value: unknown): boolean {
  if (!value || typeof value !== 'object') return false;
  return findSensitive(value, new WeakSet());
}

function findSensitive(value: unknown, seen: WeakSet<object>): boolean {
  if (!value || typeof value !== 'object') return false;
  if (seen.has(value as object)) return false;
  seen.add(value as object);
  if (Array.isArray(value)) return value.some((v) => findSensitive(v, seen));
  for (const [key, v] of Object.entries(value as Record<string, unknown>)) {
    if (keyIsSensitive(key)) return true;
    if (findSensitive(v, seen)) return true;
  }
  return false;
}
