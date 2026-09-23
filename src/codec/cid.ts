/**
 * Content IDentifier (CID) parsing/construction.
 *
 * Accepted:
 *   CIDv0  — base58btc text, 34 bytes 0x12 0x20 <sha256>, always dag-pb
 *   CIDv1  — multibase 'b' (lowercase RFC 4648 base32, no padding),
 *            codec in {dag-pb=0x70, raw=0x55}, multihash sha2-256
 *
 * Anything else (other multibase letter, CIDv2, other codec/hash) is rejected.
 */
import { base32Decode, base32LowerEncode, base58btcDecode, base58btcEncode } from './base.js';
import {
  decodeMultihash,
  readVarint,
  sha256Multihash,
  concat,
  type Multihash,
} from './multihash.js';

export const CODEC_DAG_PB = 0x70;
export const CODEC_RAW = 0x55;
const SUPPORTED_CODECS = new Map<number, string>([
  [CODEC_DAG_PB, 'dag-pb'],
  [CODEC_RAW, 'raw'],
]);

export interface CID {
  version: 0 | 1;
  codec: number;
  codecName: string;
  multihash: Multihash;
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

function buildV1(codec: number, mh: Multihash): CID {
  const name = SUPPORTED_CODECS.get(codec);
  if (!name) {
    const err = new Error(`unsupported CID codec 0x${codec.toString(16)}; only dag-pb and raw are accepted`);
    (err as NodeJS.ErrnoException).code = 'E_UNSUPPORTED_CODEC';
    throw err;
  }
  return { version: 1, codec, codecName: name, multihash: mh };
}

/** Parse a CID from its text form (CIDv0 base58btc or CIDv1 multibase string). */
export function parseCID(text: string): CID {
  if (typeof text !== 'string') throw new Error('CID must be a string');
  const t = text.trim();
  if (t.length === 0) throw new Error('empty CID');

  if (t[0] === 'Q') {
    // ---- CIDv0 ----
    const bytes = base58btcDecode(t);
    if (bytes.length !== 34 || bytes[0] !== 0x12 || bytes[1] !== 0x20) {
      throw new Error('CIDv0 must decode to exactly 34 bytes 0x12 0x20 <32-byte sha256>');
    }
    const mh = decodeMultihash(bytes);
    return { version: 0, codec: CODEC_DAG_PB, codecName: 'dag-pb', multihash: mh };
  }

  // ---- CIDv1: must carry the 'b' multibase prefix ----
  if (t[0] !== 'b') {
    const err = new Error(
      `unsupported multibase encoding ${JSON.stringify(t[0])}; only CIDv1 lowercase base32 ('b') and CIDv0 base58btc are accepted`,
    );
    (err as NodeJS.ErrnoException).code = 'E_UNSUPPORTED_ENCODING';
    throw err;
  }
  const bytes = base32Decode(t.slice(1), { rejectUpperCase: true });
  const ver = readVarint(bytes, 0);
  if (ver.value !== 1) throw new Error(`unsupported CID version ${ver.value} (only 0 and 1 are accepted)`);
  const codec = readVarint(bytes, ver.next);
  const mh = decodeMultihash(bytes.subarray(codec.next));
  return buildV1(codec.value, mh);
}

/** Binary form used inside dag-pb Link messages (v0 = bare multihash, v1 = full CID bytes). */
export function cidToLinkBytes(cid: CID): Uint8Array {
  if (cid.version === 0) return cid.multihash.bytes;
  return concat([encodeVarint(1), encodeVarint(cid.codec), cid.multihash.bytes]);
}

export function cidFromLinkBytes(bytes: Uint8Array): CID {
  // dag-pb links may reference either bare multihash (CIDv0) or full CIDv1 bytes.
  if (bytes.length >= 2 && bytes[0] === 0x12 && bytes[1] === 0x20) {
    if (bytes.length !== 34) throw new Error('bare-multihash link must be 34 bytes');
    return { version: 0, codec: CODEC_DAG_PB, codecName: 'dag-pb', multihash: decodeMultihash(bytes) };
  }
  const ver = readVarint(bytes, 0);
  if (ver.value !== 1) throw new Error('unsupported CID version inside dag-pb link');
  const codec = readVarint(bytes, ver.next);
  return buildV1(codec.value, decodeMultihash(bytes.subarray(codec.next)));
}

export function cidV1Bytes(cid: CID): Uint8Array {
  return concat([encodeVarint(1), encodeVarint(cid.codec), cid.multihash.bytes]);
}

/** Canonical text form: CIDv0 stays base58btc; CIDv1 is b + lowercase base32. */
export function cidToString(cid: CID): string {
  if (cid.version === 0) return base58btcEncode(cid.multihash.bytes);
  return 'b' + base32LowerEncode(cidV1Bytes(cid));
}

/** Equivalent v1 representation of a v0 CID (same digest, codec dag-pb). */
export function cidAsV1(cid: CID): CID {
  if (cid.version === 1) return cid;
  return { version: 1, codec: CODEC_DAG_PB, codecName: 'dag-pb', multihash: cid.multihash };
}

export function cidEquals(a: CID, b: CID): boolean {
  const x = cidAsV1(a);
  const y = cidAsV1(b);
  return (
    x.codec === y.codec &&
    Buffer.from(x.multihash.bytes).equals(Buffer.from(y.multihash.bytes))
  );
}

export function cidFromData(codec: number, data: Uint8Array): CID {
  return buildV1(codec, sha256Multihash(data));
}
