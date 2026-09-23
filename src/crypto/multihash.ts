/**
 * Multihash parsing/construction and real digest computation.
 *
 * Only sha2-256 with a 32-byte digest is accepted: identity multihashes
 * (self-attesting content), truncated digests and any other hash functions
 * are rejected so verification cannot be reduced to a string-shape check.
 *
 * @see https://github.com/multiformats/multihash
 */
import { createHash } from "node:crypto";
import { encodeVarint, readVarint } from "./varint";

/** multihash code for sha2-256. */
export const SHA2_256_CODE = 0x12;
export const SHA2_256_LEN = 32;

export interface Multihash {
  /** Hash function code (always SHA2_256_CODE when accepted). */
  code: number;
  /** Raw digest bytes. */
  digest: Buffer;
  /** Serialized multihash bytes `<code><length><digest>`. */
  bytes: Buffer;
}

export function sha256(data: Buffer): Buffer {
  return createHash("sha256").update(data).digest();
}

export function multihashSha256(data: Buffer): Multihash {
  const digest = sha256(data);
  const bytes = Buffer.concat([encodeVarint(SHA2_256_CODE), encodeVarint(SHA2_256_LEN), digest]);
  return { code: SHA2_256_CODE, digest, bytes };
}

/**
 * Parse and strictly validate a multihash.
 * @throws Error if the hash function is unknown, the digest length is not
 *         exactly 32 bytes, the encoding is non-minimal, or bytes trail.
 */
export function parseMultihash(buf: Buffer, offset = 0): Multihash {
  const codeRead = readVarint(buf, offset);
  const lenRead = readVarint(buf, offset + codeRead.length);
  const start = offset + codeRead.length + lenRead.length;
  if (start + lenRead.value !== buf.length - offset) {
    throw new Error(
      `multihash: length mismatch (header says ${lenRead.value}, payload is ${
        buf.length - offset - start
      })`
    );
  }
  const digest = buf.subarray(start, start + lenRead.value);
  if (codeRead.value !== SHA2_256_CODE) {
    throw new Error(
      `multihash: unsupported hash function code 0x${codeRead.value.toString(
        16
      )}; only sha2-256 (0x12) is accepted`
    );
  }
  if (lenRead.value !== SHA2_256_LEN) {
    throw new Error(
      `multihash: sha2-256 digest must be ${SHA2_256_LEN} bytes, got ${lenRead.value}`
    );
  }
  return {
    code: SHA2_256_CODE,
    digest: Buffer.from(digest),
    bytes: Buffer.from(buf.subarray(offset, buf.length)),
  };
}

/**
 * Compute the sha2-256 digest of real content and compare it with the digest
 * claimed inside a multihash, using constant-time comparison.
 */
export function verifyMultihash(content: Buffer, mh: Multihash): boolean {
  if (mh.code !== SHA2_256_CODE || mh.digest.length !== SHA2_256_LEN) return false;
  const actual = sha256(content);
  if (actual.length !== mh.digest.length) return false;
  let diff = 0;
  for (let i = 0; i < actual.length; i++) diff |= actual[i] ^ mh.digest[i];
  return diff === 0;
}
