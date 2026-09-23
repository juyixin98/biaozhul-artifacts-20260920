/**
 * Multihash (https://multiformats.io/multihash/) support.
 *
 * Deliberately limited to the hash algorithms NFT metadata DAGs actually use:
 *   code 0x12, length 32  ->  SHA2-256 (real digest via node:crypto)
 *
 * Every other code (including identity 0x00, sha1, sha512, blake*) is rejected
 * with E_UNSUPPORTED_MULTIHASH so callers cannot trick verification by merely
 * naming an unsupported code.
 */
import { createHash } from 'node:crypto';

export const MH_SHA2_256 = 0x12;
export const SHA2_256_LEN = 32;

export interface Multihash {
  code: number;
  name: string;
  length: number;
  digest: Uint8Array;
  /** Serialized multihash bytes: <varint code><varint length><digest> */
  bytes: Uint8Array;
}

export function readVarint(buf: Uint8Array, offset: number): { value: number; next: number } {
  let result = 0;
  let shift = 0;
  let pos = offset;
  for (;;) {
    if (pos >= buf.length) throw new Error('truncated multihash varint');
    if (shift > 28) throw new Error('multihash varint too large');
    const byte = buf[pos]!;
    result |= (byte & 0x7f) << shift;
    pos++;
    if ((byte & 0x80) === 0) break;
    shift += 7;
  }
  return { value: result >>> 0, next: pos };
}

function encodeVarint(value: number): Uint8Array {
  const out: number[] = [];
  let v = value;
  while (v >= 0x80) {
    out.push((v & 0x7f) | 0x80);
    v >>>= 7;
  }
  out.push(v);
  return Uint8Array.from(out);
}

export function decodeMultihash(bytes: Uint8Array): Multihash {
  const code = readVarint(bytes, 0);
  const len = readVarint(bytes, code.next);
  if (len.value === 0) throw new Error('multihash length is zero');
  const digestStart = len.next;
  const digestEnd = digestStart + len.value;
  if (digestEnd !== bytes.length) {
    throw new Error(
      `multihash length ${len.value} does not match remaining ${bytes.length - digestStart} bytes`,
    );
  }
  const digest = bytes.subarray(digestStart, digestEnd);
  const supported = code.value === MH_SHA2_256 && len.value === SHA2_256_LEN;
  if (!supported) {
    const err = new Error(
      `unsupported multihash code=0x${code.value.toString(16)} length=${len.value}; ` +
        'only sha2-256 (code 0x12, length 32) is accepted',
    );
    (err as NodeJS.ErrnoException).code = 'E_UNSUPPORTED_MULTIHASH';
    throw err;
  }
  return {
    code: code.value,
    name: 'sha2-256',
    length: len.value,
    digest: Uint8Array.from(digest),
    bytes: Uint8Array.from(bytes),
  };
}

export function sha256Multihash(data: Uint8Array): Multihash {
  const digest = createHash('sha256').update(data).digest();
  const bytes = concat([
    encodeVarint(MH_SHA2_256),
    encodeVarint(SHA2_256_LEN),
    digest,
  ]);
  return { code: MH_SHA2_256, name: 'sha2-256', length: SHA2_256_LEN, digest, bytes };
}

/** Real cryptographic comparison of block bytes against the claimed multihash. */
export function verifyMultihash(data: Uint8Array, expected: Multihash): { ok: boolean; actual: string; claimed: string } {
  const actual = createHash('sha256').update(data).digest();
  const claimedHex = toHex(expected.digest);
  const actualHex = toHex(actual);
  return { ok: actualHex === claimedHex, actual: actualHex, claimed: claimedHex };
}

export function toHex(b: Uint8Array): string {
  return Buffer.from(b).toString('hex');
}

export function concat(parts: Uint8Array[]): Uint8Array {
  let len = 0;
  for (const p of parts) len += p.length;
  const out = new Uint8Array(len);
  let off = 0;
  for (const p of parts) {
    out.set(p, off);
    off += p.length;
  }
  return out;
}
