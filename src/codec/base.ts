/**
 * Base encodings used by content identifiers.
 *
 * - base58btc: used by CIDv0 strings (Bitcoin alphabet, no multibase prefix)
 * - base32 lower: used by CIDv1 default strings ("b" multibase prefix, RFC 4648 lowercase)
 *
 * Implemented from the alphabet definitions — no string-shape only checks;
 * every decode round-trips through base->digits->bytes with carry arithmetic.
 */

const BASE58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
const BASE58_INDEX = new Map<string, number>();
for (let i = 0; i < BASE58_ALPHABET.length; i++) BASE58_INDEX.set(BASE58_ALPHABET[i]!, i);

export function base58btcDecode(str: string): Uint8Array {
  if (str.length === 0) throw new Error('empty base58btc string');
  // Leading '1's correspond to leading zero bytes.
  let zeros = 0;
  while (zeros < str.length && str[zeros] === '1') zeros++;

  const size = Math.ceil((str.length - zeros) * 733 / 1000) + 1;
  const b256 = new Uint8Array(size);
  for (let i = zeros; i < str.length; i++) {
    const digit = BASE58_INDEX.get(str[i]!);
    if (digit === undefined) {
      throw new Error(`invalid base58btc character ${JSON.stringify(str[i])} at position ${i}`);
    }
    let carry = digit;
    for (let j = size - 1; j >= 0; j--) {
      carry += 58 * b256[j]!;
      b256[j] = carry & 0xff;
      carry >>= 8;
    }
    if (carry !== 0) throw new Error('base58btc carry overflow');
  }

  let it = 0;
  while (it < size && b256[it] === 0) it++;
  const out = new Uint8Array(zeros + (size - it));
  out.set(b256.subarray(it), zeros);
  return out;
}

export function base58btcEncode(bytes: Uint8Array): string {
  let zeros = 0;
  while (zeros < bytes.length && bytes[zeros] === 0) zeros++;

  const size = Math.ceil((bytes.length - zeros) * 1366 / 1000) + 1;
  const b58 = new Uint8Array(size);
  for (let i = zeros; i < bytes.length; i++) {
    let carry = bytes[i]!;
    for (let j = size - 1; j >= 0; j--) {
      carry += 256 * b58[j]!;
      b58[j] = carry % 58;
      carry = Math.floor(carry / 58);
    }
    if (carry !== 0) throw new Error('base58btc encode carry overflow');
  }

  let it = 0;
  while (it < size && b58[it] === 0) it++;
  let out = '1'.repeat(zeros);
  for (; it < size; it++) out += BASE58_ALPHABET[b58[it]!];
  return out;
}

// RFC 4648 base32 alphabet; CIDv1 text uses the no-padding lowercase variant.
const B32_LOWER = 'abcdefghijklmnopqrstuvwxyz234567';
const B32_UPPER = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
const B32_INDEX = new Map<string, number>();
for (let i = 0; i < B32_LOWER.length; i++) {
  B32_INDEX.set(B32_LOWER[i]!, i);
  B32_INDEX.set(B32_UPPER[i]!, i);
}

export function base32Decode(str: string, opts: { rejectUpperCase: boolean }): Uint8Array {
  if (str.length === 0) return new Uint8Array(0);
  if (str.includes('=')) throw new Error('padded base32 is not a valid CID text encoding');
  // Canonical CIDv1 base32 is lowercase; uppercase is a different (non-default)
  // multibase ("B") and must not be silently accepted.
  if (opts.rejectUpperCase && /[A-Z]/.test(str)) {
    throw new Error('uppercase base32 letters are not valid under lowercase multibase "b"');
  }
  let bits = 0;
  let value = 0;
  const out: number[] = [];
  for (let i = 0; i < str.length; i++) {
    const digit = B32_INDEX.get(str[i]!);
    if (digit === undefined) {
      throw new Error(`invalid base32 character ${JSON.stringify(str[i])} at position ${i}`);
    }
    value = (value << 5) | digit;
    bits += 5;
    if (bits >= 8) {
      bits -= 8;
      out.push((value >>> bits) & 0xff);
    }
  }
  // Canonical-length check: the trailing partial group must be one of the
  // standard 2/4/5/7-bit residues; anything else means bits were invented.
  if (bits !== 0) {
    if (str.length % 8 === 1 || str.length % 8 === 3 || str.length % 8 === 6) {
      throw new Error('non-canonical base32 length');
    }
    // Residue bits of a canonical encoding are zero.
    if ((value & ((1 << bits) - 1)) !== 0) throw new Error('non-zero base32 residue bits');
  }
  return Uint8Array.from(out);
}

export function base32LowerEncode(bytes: Uint8Array): string {
  let bits = 0;
  let value = 0;
  let out = '';
  for (const b of bytes) {
    value = (value << 8) | b;
    bits += 8;
    while (bits >= 5) {
      out += B32_LOWER[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) out += B32_LOWER[(value << (5 - bits)) & 31];
  return out;
}
