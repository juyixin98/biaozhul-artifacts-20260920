import { createHash, randomBytes } from 'crypto';

// Unambiguous alphabet (no 0/O/1/I/L) for 6-char join codes.
const CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
const CODE_LENGTH = 6;

export function generateJoinCode(): string {
  const bytes = randomBytes(CODE_LENGTH);
  let code = '';
  for (let i = 0; i < CODE_LENGTH; i++) {
    code += CODE_ALPHABET[bytes[i] % CODE_ALPHABET.length];
  }
  return code;
}

// Stable hash of the command body so a retried requestId with different
// arguments is rejected as a conflict instead of silently replaying.
export function hashBody(body: unknown): string {
  return createHash('sha256').update(canonicalString(body)).digest('hex');
}

function canonicalString(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalString).join(',')}]`;
  const obj = value as Record<string, unknown>;
  const keys = Object.keys(obj).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalString(obj[k])}`).join(',')}}`;
}
