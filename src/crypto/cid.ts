/**
 * CID (Content IDentifier) parsing and construction — CIDv0 and CIDv1 only.
 *
 * Supported forms (an intentionally small, explicit whitelist):
 *   - CIDv0: base58btc of a sha2-256 multihash, i.e. "Qm…" (dag-pb, 34 bytes)
 *   - CIDv1 with multibase 'b' (RFC4648 lowercase unpadded base32) or
 *     multibase 'z' (base58btc)
 *
 * Rejected: any other multibase (base16/36/64/…), CID versions other than
 * 0/1, unknown multicodecs, non-sha2-256 multihashes, truncated digests,
 * identity hashes, non-canonical encodings and trailing bytes.
 *
 * @see https://github.com/multiformats/cid
 */
import { decodeBase58, encodeBase58 } from "./base58";
import { decodeBase32, encodeBase32 } from "./base32";
import {
  Multihash,
  SHA2_256_LEN,
  multihashSha256,
  parseMultihash,
} from "./multihash";
import { encodeVarint, readVarint } from "./varint";

/** multicodec codes accepted on input. */
export const CODEC_DAG_PB = 0x70;
export const CODEC_RAW = 0x55;
export const CODEC_JSON = 0x0200;

const ALLOWED_CODECS = new Set([CODEC_DAG_PB, CODEC_RAW, CODEC_JSON]);

export type CidVersion = 0 | 1;
export type MultibaseEncoding = "base58btc" | "base32lower";

export interface Cid {
  version: CidVersion;
  /** multicodec of the content (dag-pb for v0). */
  codec: number;
  multihash: Multihash;
  /** Raw CID bytes (v0 is just the multihash). */
  bytes: Buffer;
  /** Canonical text form (v0: base58btc; v1: base32lower 'b' multibase). */
  canonical: string;
  /** Multibase of the input string this CID was parsed from. */
  inputEncoding: MultibaseEncoding;
}

/** Build a CIDv1 for real content, hashing it with sha2-256. */
export function makeCidV1(codec: number, content: Buffer): Cid {
  const mh = multihashSha256(content);
  const bytes = Buffer.concat([
    encodeVarint(1),
    encodeVarint(codec),
    mh.bytes,
  ]);
  const cid: Cid = {
    version: 1,
    codec,
    multihash: mh,
    bytes,
    canonical: "",
    inputEncoding: "base32lower",
  };
  cid.canonical = "b" + encodeBase32(bytes);
  return cid;
}

/** Build a CIDv0 (implicit dag-pb) for real content. */
export function makeCidV0(content: Buffer): Cid {
  const mh = multihashSha256(content);
  const cid: Cid = {
    version: 0,
    codec: CODEC_DAG_PB,
    multihash: mh,
    bytes: mh.bytes,
    canonical: "",
    inputEncoding: "base58btc",
  };
  cid.canonical = encodeBase58(mh.bytes);
  return cid;
}

function fromParts(
  version: CidVersion,
  codec: number,
  mh: Multihash,
  bytes: Buffer,
  inputEncoding: MultibaseEncoding
): Cid {
  const cid: Cid = { version, codec, multihash: mh, bytes, canonical: "", inputEncoding };
  cid.canonical =
    version === 0 ? encodeBase58(bytes) : "b" + encodeBase32(bytes);
  return cid;
}

/**
 * Parse a CID string with full structural and canonical validation.
 * Throws Error with a descriptive message on every rejection.
 */
export function parseCid(text: string): Cid {
  if (typeof text !== "string") throw new Error("CID: input is not a string");
  if (text.length === 0) throw new Error("CID: empty string");

  // ---- CIDv0: always starts with "Qm" (0x12 0x20 in base58btc) ----
  if (text[0] === "Q") {
    if (!text.startsWith("Qm")) {
      throw new Error("CIDv0: must start with 'Qm' (sha2-256 multihash prefix)");
    }
    let raw: Buffer;
    try {
      raw = decodeBase58(text);
    } catch (e) {
      throw new Error(`CIDv0: invalid base58btc: ${(e as Error).message}`);
    }
    if (raw.length !== 2 + SHA2_256_LEN) {
      throw new Error(
        `CIDv0: expected ${2 + SHA2_256_LEN} bytes, got ${raw.length}`
      );
    }
    const mh = parseMultihash(raw);
    const cid = fromParts(0, CODEC_DAG_PB, mh, raw, "base58btc");
    // Canonical check: re-encoding must reproduce the exact input.
    if (cid.canonical !== text) {
      throw new Error("CIDv0: non-canonical base58btc encoding");
    }
    return cid;
  }

  // ---- CIDv1: first char is the multibase prefix ----
  const prefix = text[0];
  let encoding: MultibaseEncoding;
  let cidBytes: Buffer;
  switch (prefix) {
    case "b":
      encoding = "base32lower";
      try {
        cidBytes = decodeBase32(text.slice(1));
      } catch (e) {
        throw new Error(`CIDv1(base32lower): ${(e as Error).message}`);
      }
      break;
    case "z":
      encoding = "base58btc";
      try {
        cidBytes = decodeBase58(text.slice(1));
      } catch (e) {
        throw new Error(`CIDv1(base58btc): ${(e as Error).message}`);
      }
      break;
    default:
      throw new Error(
        `unsupported multibase prefix '${prefix}': only CIDv0 (Qm…) and CIDv1 ` +
          "with 'b' (base32lower) or 'z' (base58btc) are accepted"
      );
  }

  const versionRead = readVarint(cidBytes, 0);
  if (versionRead.value !== 1) {
    throw new Error(`CIDv1: unsupported CID version ${versionRead.value} (only 0 and 1)`);
  }
  const codecRead = readVarint(cidBytes, versionRead.length);
  if (!ALLOWED_CODECS.has(codecRead.value)) {
    throw new Error(
      `CIDv1: unsupported multicodec 0x${codecRead.value.toString(
        16
      )}; only raw (0x55), json (0x0200) and dag-pb (0x70) are accepted`
    );
  }
  // dag-pb in v1 (0x70 with version 1) is structurally valid multiformats,
  // but this service never produces it and treats metadata as json; reject it
  // here so supported encodings stay explicit and minimal.
  if (codecRead.value === CODEC_DAG_PB) {
    throw new Error(
      "CIDv1: dag-pb (0x70) CIDv1 is not accepted; use CIDv0 for dag-pb or json/raw CIDv1"
    );
  }
  const mhStart = versionRead.length + codecRead.length;
  const mh = parseMultihash(cidBytes.subarray(mhStart));

  const cid = fromParts(1, codecRead.value, mh, Buffer.from(cidBytes), encoding);
  // Canonical encoding check: the decoded bytes, encoded canonically, must
  // equal the input for the input's own multibase.
  const expectedForEncoding =
    encoding === "base32lower" ? cid.canonical : "z" + encodeBase58(cidBytes);
  if (expectedForEncoding !== text) {
    throw new Error(
      "CIDv1: non-canonical encoding (re-encoding the decoded bytes does not reproduce the input)"
    );
  }
  return cid;
}

/** True iff text parses as an accepted CID. */
export function isCid(text: unknown): text is string {
  return typeof text === "string" && isValidCid(text);
}

export function isValidCid(text: string): boolean {
  try {
    parseCid(text);
    return true;
  } catch {
    return false;
  }
}
