/**
 * LEB128 unsigned varint (the unsigned-variable integer used by multiformats).
 *
 * @see https://github.com/multiformats/unsigned-varint
 */

/** Encode a non-negative safe integer into LEB128 bytes. */
export function encodeVarint(value: number): Buffer {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error(`varint: value out of range: ${value}`);
  }
  const out: number[] = [];
  let v = value;
  do {
    let byte = v & 0x7f;
    v = Math.floor(v / 128);
    if (v > 0) byte |= 0x80;
    out.push(byte);
  } while (v > 0);
  return Buffer.from(out);
}

export interface VarintRead {
  value: number;
  /** Number of bytes consumed. */
  length: number;
}

/**
 * Decode a minimal LEB128 varint. Trailing zero groups (non-minimal encoding)
 * and values beyond MAX_SAFE_INTEGER are rejected.
 */
export function readVarint(buf: Buffer, offset = 0): VarintRead {
  let result = 0;
  let shift = 0;
  let i = offset;
  for (;;) {
    if (i >= buf.length) throw new Error("varint: unexpected end of input");
    const byte = buf[i++];
    const part = byte & 0x7f;
    // Range check before the arithmetic (shift==49, part may contribute at
    // most 4 bits without exceeding 2^53-1).
    if (shift === 49 && part > 0x0f) {
      throw new Error("varint: integer exceeds safe integer range");
    }
    if (shift > 49) {
      throw new Error("varint: integer exceeds safe integer range");
    }
    result += part * Math.pow(2, shift);
    if ((byte & 0x80) === 0) {
      // Minimal-encoding check: the final byte of a multi-byte varint must
      // not be zero (that zero group should have been omitted).
      if (i - offset > 1 && byte === 0) throw new Error("varint: non-minimal encoding");
      return { value: result, length: i - offset };
    }
    shift += 7;
    if (shift > 56) throw new Error("varint: integer too large");
  }
}
