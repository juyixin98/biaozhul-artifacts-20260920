import { createHash } from "node:crypto";
import { LIMITS } from "../config";
import { parseCid } from "../crypto/cid";
import { verifyMultihash } from "../crypto/multihash";
import { AppError } from "../errors";
import { BlockStore } from "../store/blockstore";

/**
 * Strict standard base64 decoder (with padding). URL-safe variants and
 * malformed padding are rejected explicitly rather than tolerated.
 */
export function decodeBase64Strict(input: string): Buffer {
  if (typeof input !== "string") {
    throw new AppError("BAD_REQUEST", "data_base64 must be a string");
  }
  if (input.length === 0) return Buffer.alloc(0);
  if (input.includes("-") || input.includes("_")) {
    throw new AppError(
      "INVALID_BASE64",
      "data_base64 must use standard base64 alphabet (+,/), not base64url (-,_)"
    );
  }
  // Standard base64 requires length % 4 === 0 with correct '=' padding.
  if (input.length % 4 !== 0) {
    throw new AppError("INVALID_BASE64", "data_base64 length must be a multiple of 4 with padding");
  }
  if (/[^A-Za-z0-9+/=]/.test(input)) {
    throw new AppError("INVALID_BASE64", "data_base64 contains non-base64 characters");
  }
  const padIndex = input.indexOf("=");
  if (padIndex !== -1) {
    if (!/^=+$/.test(input.slice(padIndex)) || input.length - padIndex > 2) {
      throw new AppError("INVALID_BASE64", "data_base64 has malformed padding");
    }
  }
  const decoded = Buffer.from(input, "base64");
  // Round-trip: Node tolerates some malformed input; re-encode and compare.
  if (decoded.toString("base64") !== input) {
    throw new AppError("INVALID_BASE64", "data_base64 is not canonical (failed base64 round-trip)");
  }
  return decoded;
}

export interface IngestResult {
  cid: string;
  existed: boolean;
  size: number;
  computedDigest: string;
}

export class BlockService {
  constructor(private readonly blocks: BlockStore) {}

  /**
   * Ingest a claimed (cid, bytes) pair. The bytes are re-hashed here and must
   * satisfy the CID's multihash BEFORE anything is persisted — a tampered
   * payload never enters the store.
   */
  ingest(cidText: string, data: Buffer): IngestResult {
    if (data.length > LIMITS.MAX_BLOCK_BYTES) {
      throw new AppError(
        "BLOCK_TOO_LARGE",
        `block is ${data.length} bytes, limit is ${LIMITS.MAX_BLOCK_BYTES}`
      );
    }

    let cid;
    try {
      cid = parseCid(cidText);
    } catch (e) {
      throw new AppError("UNSUPPORTED_CID", (e as Error).message, { value: cidText });
    }

    const computed = sha256Hex(data);
    if (!verifyMultihash(data, cid.multihash)) {
      throw new AppError(
        "CONTENT_MISMATCH",
        "content-address check failed: sha2-256 of the provided bytes does not match the CID multihash digest",
        {
          cid: cid.canonical,
          claimedDigest: cid.multihash.digest.toString("hex"),
          computedDigest: computed,
          size: data.length,
        }
      );
    }

    const { cid: canonical, existed } = this.blocks.put(cid, data);
    return { cid: canonical, existed, size: data.length, computedDigest: computed };
  }
}

function sha256Hex(data: Buffer): string {
  return createHash("sha256").update(data).digest("hex");
}
