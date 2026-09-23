/**
 * RFC 4648 base32 codec (lowercase, unpadded — the `b` multibase encoding).
 *
 * Decoding is strict: non-alphabet characters (including padding after data
 * and uppercase letters, which denote the separate `B` multibase) are errors.
 *
 * @see https://datatracker.ietf.org/doc/html/rfc4648#section-6
 */

const ALPHABET = "abcdefghijklmnopqrstuvwxyz234567";
const BITS = 5;

const DECODE: Int8Array = (() => {
  const table = new Int8Array(128).fill(-1);
  for (let i = 0; i < ALPHABET.length; i++) table[ALPHABET.charCodeAt(i)] = i;
  return table;
})();

export function encodeBase32(data: Buffer): string {
  let bits = 0;
  let value = 0;
  let out = "";
  for (const byte of data) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= BITS) {
      out += ALPHABET[(value >>> (bits - BITS)) & 0x1f];
      bits -= BITS;
    }
  }
  if (bits > 0) {
    out += ALPHABET[(value << (BITS - bits)) & 0x1f];
  }
  return out;
}

export function decodeBase32(str: string): Buffer {
  if (str.length === 0) return Buffer.alloc(0);

  let bits = 0;
  let value = 0;
  const out: number[] = [];
  for (const ch of str) {
    const code = ch.charCodeAt(0);
    const idx = code < 128 ? DECODE[code] : -1;
    if (idx < 0) {
      throw new Error(
        `base32: invalid character '${ch}' (only lowercase unpadded RFC4648 base32 is accepted)`
      );
    }
    value = (value << BITS) | idx;
    bits += BITS;
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
      value &= (1 << bits) - 1;
    }
  }
  // The trailing partial group is only valid as canonical padding:
  // 2/4/5/7 chars after the last complete 8-char group. Bits left over must
  // be zero, otherwise canonical data was lost (e.g. "b" with junk low bits).
  const rem = str.length % 8;
  if (rem === 1 || rem === 3 || rem === 6) {
    throw new Error("base32: invalid length for canonical unpadded base32");
  }
  if (bits > 0 && (value & ((1 << bits) - 1)) !== 0) {
    throw new Error("base32: non-canonical trailing bits");
  }
  return Buffer.from(out);
}
