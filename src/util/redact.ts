/**
 * Redaction used before any user-supplied value touches the ledger or logs.
 * Allow-list of sensitive key name patterns (matched case-insensitively).
 */

const SENSITIVE_KEY_PATTERN =
  /^(secret|password|passwd|token|api[_-]?key|access[_-]?key|private[_-]?key)$/i;

export const REDACTED = "***redacted***";

export function redactValue(value: unknown, depth = 0): unknown {
  if (depth > 6) return "[truncated]";
  if (Array.isArray(value)) {
    return value.map((item) => redactValue(item, depth + 1));
  }
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, inner] of Object.entries(
      value as Record<string, unknown>,
    )) {
      out[key] = SENSITIVE_KEY_PATTERN.test(key)
        ? REDACTED
        : redactValue(inner, depth + 1);
    }
    return out;
  }
  return value;
}

/** Stable compact JSON of a redacted copy, safe to persist and to log. */
export function redactedJson(value: unknown): string {
  return JSON.stringify(redactValue(value));
}

export function isSensitiveKey(key: string): boolean {
  return SENSITIVE_KEY_PATTERN.test(key);
}
