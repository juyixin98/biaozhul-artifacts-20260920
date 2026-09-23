/**
 * Base58btc codec — the Bitcoin alphabet used by CIDv0 and the `z` multibase.
 *
 * Real big-integer arithmetic via JS BigInt: bytes are treated as one big
 * endian integer and converted to/from base 58. Leading zero bytes map to
 * leading '1' digits.
 *
 * @see https://datatracker.ilpolitico.it/base58/  (spec summary)
 * @see https://en.bitcoin.it/wiki/Base58Check_encoding
 */

const ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
const BASE = 58n;

const REVERSE: Int8Array = (() => {
  const table = new Int8Array(128).fill(-1);
  for (let i = 0; i < ALPHABET.length; i++) table[ALPHABET.charCodeAt(i)] = i;
  return table;
})();

export function encodeBase58(bytes: Buffer): string {
  let num = BigInt("0x" + (bytes.toString("hex") || "0"));
  let digits = "";
  while (num > 0n) {
    const rem = Number(num % BASE);
    num = num / BASE;
    digits = ALPHABET[rem] + digits;
  }
  // Each leading zero byte becomes a leading '1' digit.
  let pad = 0;
  while (pad < bytes.length && bytes[pad] === 0) {
    digits = ALPHABET[0] + digits;
    pad++;
  }
  return digits;
}

export function decodeBase58(str: string): Buffer {
  if (str.length === 0) throw new Error("base58: empty input");
  let num = 0n;
  for (const ch of str) {
    const code = ch.charCodeAt(0);
    const value = code < 128 ? REVERSE[code] : -1;
    if (value < 0) throw new Error(`base58: invalid character '${ch}'`);
    num = num * BASE + BigInt(value);
  }
  // Count leading '1' zero-byte markers.
  let zeros = 0;
  while (zeros < str.length && str[zeros] === ALPHABET[0]) zeros++;

  const bytes: number[] = [];
  while (num > 0n) {
    bytes.push(Number(num % 256n));
    num /= 256n;
  }
  const body = Buffer.from(bytes.reverse());
  return Buffer.concat([Buffer.alloc(zeros), body]);
}
