/**
 * DAG verification engine.
 *
 * For every node in the metadata DAG it performs REAL checks against the
 * stored bytes and records the exact cryptographic/protocol basis:
 *
 *  1. CID structure      — version, multibase, multicodec, multihash code
 *  2. content addressing — recompute sha2-256 over the actual bytes and
 *                          compare with the digest embedded in the CID
 *  3. encoding layer     — raw: magic-byte media sniff + declared size/type
 *                          json: strict UTF-8 decode + strict JSON parse +
 *                          schema, with recursive link verification
 *  4. reference integrity— every declared image/link CID resolves to a stored
 *                          block of the expected codec; cycles are rejected.
 */
import { LIMITS } from "./config";
import {
  Cid,
  CODEC_JSON,
  CODEC_RAW,
  parseCid,
} from "./crypto/cid";
import { sha256, verifyMultihash } from "./crypto/multihash";
import { AppError } from "./errors";
import { decodeUtf8Strict } from "./codec/utf8";
import { sniffMediaType } from "./codec/media-sniff";
import { parseStrictJson, JsonValue } from "./codec/json-parse";
import { ParsedMetadata, validateMetadataShape } from "./codec/metadata";
import { BlockStore } from "./store/blockstore";

export interface Check {
  /** Stable machine-readable check id. */
  id: string;
  /** Human-readable statement of what was checked and how it was proven. */
  detail: string;
  ok: boolean;
}

export interface NodeEvidence {
  /** Dotted JSON path of the reference, "$" for the root. */
  refPath: string;
  /** Label from metadata `links[].name`, when present. */
  name?: string;
  cid: string;
  cidVersion: 0 | 1;
  multibase: string;
  multicodecName: "raw" | "json" | "dag-pb";
  multicodecCode: number;
  hashAlgorithm: "sha2-256";
  hashCode: number;
  /** Digest claimed by the CID (hex). */
  claimedDigest: string;
  /** Digest recomputed over the stored bytes (hex). */
  computedDigest: string;
  contentAddressValid: boolean;
  sizeActual: number;
  /** Declared image_size from the parent metadata (image nodes only). */
  sizeDeclared?: number;
  sizeMatches?: boolean;
  mediaTypeDeclared?: string;
  mediaTypeDetected?: string;
  mediaTypeMatches?: boolean;
  jsonEncodingValid?: boolean;
  /** This node was already verified at another path (diamond DAG). */
  alreadyVerified?: boolean;
  checks: Check[];
  children: NodeEvidence[];
}

export interface VerificationResult {
  tokenName: string;
  version: number | null;
  rootCid: string;
  nodesVisited: number;
  tree: NodeEvidence;
}

interface MediaExpectation {
  declaredType: string;
  declaredSize: number;
}

export class DagVerifier {
  /**
   * @param blocks content block store
   * @param trustStoredHashes test-only: skip the byte-vs-multihash
   *        recomputation so that cycle-guard behavior can be exercised on a
   *        fabricated store. Genuine cycles are mathematically unconstructible
   *        with real content addressing (each CID commits to its successors),
   *        hence the seam. Never used on the HTTP path.
   */
  constructor(
    private readonly blocks: BlockStore,
    private readonly trustStoredHashes = false
  ) {}

  /**
   * Verify a token metadata root (must be json codec, must carry version).
   * @param expectedVersion when registering revision n, the JSON "version"
   *                        field must equal n.
   */
  verifyRoot(rootCidText: string, expectedVersion?: number): VerificationResult {
    const rootCid = parseCidOrThrow(rootCidText);
    if (rootCid.codec !== CODEC_JSON) {
      throw new AppError(
        "UNSUPPORTED_CODEC",
        `metadata root must be a json (0x0200) CIDv1 block, got ${codecName(rootCid.codec)}`,
        { cid: rootCid.canonical, codec: rootCid.codec }
      );
    }
    const ctx: WalkCtx = { stack: [], seen: new Map(), count: 0 };
    const tree = this.walk(rootCid, "$", true, ctx, undefined);
    const meta = ctx.rootMetadata!;
    if (meta.version === null) {
      throw new AppError("INVALID_METADATA", "root metadata is missing the required 'version'");
    }
    if (expectedVersion !== undefined && meta.version !== expectedVersion) {
      throw new AppError(
        "VERSION_CONFLICT",
        `metadata "version" is ${meta.version} but revision ${expectedVersion} was being registered`,
        { metadataVersion: meta.version, expectedVersion }
      );
    }
    return {
      tokenName: meta.name,
      version: meta.version,
      rootCid: rootCid.canonical,
      nodesVisited: ctx.count,
      tree,
    };
  }

  private walk(
    cid: Cid,
    refPath: string,
    isRoot: boolean,
    ctx: WalkCtx,
    media: MediaExpectation | undefined,
    name?: string
  ): NodeEvidence {
    // ---- cycle detection (reference stack, not just "seen") ----
    if (ctx.stack.includes(cid.canonical)) {
      throw new AppError(
        "DAG_CYCLE",
        `circular reference detected: ${[...ctx.stack, cid.canonical].join(" -> ")}`,
        { cycle: [...ctx.stack, cid.canonical], refPath }
      );
    }
    // ---- diamond dedupe: verify bytes once, reuse evidence skeleton ----
    const prior = ctx.seen.get(cid.canonical);
    if (prior) {
      return { ...prior, refPath, name, alreadyVerified: true, children: [] };
    }
    if (ctx.count >= LIMITS.MAX_DAG_NODES) {
      throw new AppError("BAD_REQUEST", `DAG exceeds ${LIMITS.MAX_DAG_NODES} nodes`);
    }
    if (ctx.stack.length >= LIMITS.MAX_DAG_DEPTH) {
      throw new AppError("BAD_REQUEST", `DAG exceeds max depth ${LIMITS.MAX_DAG_DEPTH}`);
    }

    const record = this.blocks.getRecord(cid.canonical);
    if (!record) {
      throw new AppError(
        "BLOCK_NOT_FOUND",
        `missing content block for ${cid.canonical} referenced at '${refPath}'`,
        { cid: cid.canonical, refPath }
      );
    }

    ctx.count++;
    ctx.stack.push(cid.canonical);
    try {
      const data = Buffer.from(record.data);
      const checks: Check[] = [];

      // ---- content addressing: REAL re-hash of stored bytes ----
      const computed = sha256(data);
      const contentAddressValid = this.trustStoredHashes
        ? true
        : verifyMultihash(data, cid.multihash);
      checks.push({
        id: "multihash-recompute",
        detail: this.trustStoredHashes
          ? "multihash recomputation skipped (test seam)"
          : `recomputed sha2-256 over ${data.length} stored bytes ` +
            `(${computed.toString("hex")}) and compared with the CID-embedded digest ` +
            `(${cid.multihash.digest.toString("hex")})`,
        ok: contentAddressValid,
      });
      if (!contentAddressValid) {
        throw new AppError(
          "CONTENT_MISMATCH",
          `content-address verification failed at '${refPath}': stored bytes do not hash to ${cid.canonical}`,
          {
            cid: cid.canonical,
            refPath,
            claimedDigest: cid.multihash.digest.toString("hex"),
            computedDigest: computed.toString("hex"),
          }
        );
      }

      const codecConsistent = record.codec === cid.codec;
      checks.push({
        id: "codec-consistency",
        detail: `stored multicodec 0x${record.codec.toString(16)} matches CID multicodec 0x${cid.codec.toString(16)} (${codecName(cid.codec)})`,
        ok: codecConsistent,
      });
      if (!codecConsistent) {
        throw new AppError("CONTENT_MISMATCH", "stored codec does not match CID codec", {
          cid: cid.canonical,
        });
      }

      checks.push({
        id: "block-size-limit",
        detail: `block size ${data.length} bytes <= limit ${LIMITS.MAX_BLOCK_BYTES}`,
        ok: data.length <= LIMITS.MAX_BLOCK_BYTES,
      });

      const ev: NodeEvidence = {
        refPath,
        name,
        cid: cid.canonical,
        cidVersion: cid.version,
        multibase: cid.version === 0 ? "implicit-base58btc" : cid.inputEncoding,
        multicodecName: codecName(cid.codec),
        multicodecCode: cid.codec,
        hashAlgorithm: "sha2-256",
        hashCode: cid.multihash.code,
        claimedDigest: cid.multihash.digest.toString("hex"),
        computedDigest: computed.toString("hex"),
        contentAddressValid,
        sizeActual: data.length,
        checks,
        children: [],
      };

      if (cid.codec === CODEC_RAW) {
        this.verifyMediaNode(data, ev, media, refPath, checks);
      } else if (cid.codec === CODEC_JSON) {
        const parsed = this.verifyJsonNode(data, ev, isRoot, checks);
        if (isRoot) ctx.rootMetadata = parsed;

        // The image reference of every json node.
        const imgCid = parseCidOrThrow(parsed.image, `${refPath}.image`);
        ev.children.push(
          this.walk(
            imgCid,
            `${refPath}.image`,
            false,
            ctx,
            { declaredType: parsed.imageType, declaredSize: parsed.imageSize }
          )
        );
        parsed.links.forEach((link, i) => {
          const linkCid = parseCidOrThrow(link.cid, `${refPath}.links[${i}].cid`);
          ev.children.push(
            this.walk(linkCid, `${refPath}.links[${i}]`, false, ctx, undefined, link.name)
          );
        });
      } else {
        throw new AppError(
          "UNSUPPORTED_CODEC",
          `dag-pb (CIDv0) blocks are not valid members of a JSON metadata DAG ('${refPath}')`,
          { cid: cid.canonical, refPath }
        );
      }

      ctx.seen.set(cid.canonical, { ...ev, children: [] });
      return ev;
    } finally {
      ctx.stack.pop();
    }
  }

  private verifyMediaNode(
    data: Buffer,
    ev: NodeEvidence,
    media: MediaExpectation | undefined,
    refPath: string,
    checks: Check[]
  ): void {
    const sniffed = sniffMediaType(data);
    if (!sniffed) {
      checks.push({
        id: "media-magic",
        detail: "no supported media magic-byte signature (PNG/JPEG/GIF/WebP) found in raw bytes",
        ok: false,
      });
      throw new AppError(
        "INVALID_METADATA",
        `media block at '${refPath}' is not a supported image type (signature sniff failed)`,
        { cid: ev.cid, refPath, firstBytes: data.subarray(0, 8).toString("hex") }
      );
    }
    checks.push({
      id: "media-magic",
      detail: `sniffed from content bytes: ${sniffed.evidence}`,
      ok: true,
    });
    ev.mediaTypeDetected = sniffed.type;

    if (!media) {
      // Raw root (ad-hoc verify): type is reported, nothing to compare to.
      ev.mediaTypeMatches = undefined;
      ev.sizeMatches = undefined;
      return;
    }
    ev.mediaTypeDeclared = media.declaredType;
    ev.mediaTypeMatches = sniffed.type === media.declaredType;
    checks.push({
      id: "media-type-match",
      detail: `declared image_type '${media.declaredType}' ${
        ev.mediaTypeMatches ? "matches" : "does NOT match"
      } byte-sniffed type '${sniffed.type}'`,
      ok: ev.mediaTypeMatches,
    });
    if (!ev.mediaTypeMatches) {
      throw new AppError(
        "INVALID_METADATA",
        `media type mismatch at '${refPath}': metadata declares '${media.declaredType}', content is '${sniffed.type}'`,
        { cid: ev.cid, refPath, declared: media.declaredType, detected: sniffed.type }
      );
    }

    ev.sizeDeclared = media.declaredSize;
    ev.sizeMatches = media.declaredSize === data.length;
    checks.push({
      id: "media-size-match",
      detail: `declared image_size ${media.declaredSize} ${
        ev.sizeMatches ? "equals" : "does NOT equal"
      } actual byte length ${data.length}`,
      ok: ev.sizeMatches,
    });
    if (!ev.sizeMatches) {
      throw new AppError(
        "INVALID_METADATA",
        `media size mismatch at '${refPath}': declared ${media.declaredSize}, actual ${data.length}`,
        { cid: ev.cid, refPath, declared: media.declaredSize, actual: data.length }
      );
    }
  }

  private verifyJsonNode(
    data: Buffer,
    ev: NodeEvidence,
    isRoot: boolean,
    checks: Check[]
  ): ParsedMetadata {
    if (data.length > LIMITS.MAX_JSON_BYTES) {
      throw new AppError(
        "BLOCK_TOO_LARGE",
        `JSON block ${ev.cid} is ${data.length} bytes, limit ${LIMITS.MAX_JSON_BYTES}`
      );
    }

    let text: string;
    try {
      text = decodeUtf8Strict(data);
    } catch (e) {
      checks.push({
        id: "utf8-decode",
        detail: `strict UTF-8 decode failed: ${(e as Error).message}`,
        ok: false,
      });
      throw new AppError(
        "INVALID_JSON",
        `JSON block ${ev.cid} is not valid UTF-8: ${(e as Error).message}`,
        { cid: ev.cid }
      );
    }
    checks.push({ id: "utf8-decode", detail: "strict UTF-8 decoding (RFC 3629) succeeded", ok: true });

    let value: JsonValue;
    try {
      value = parseStrictJson(text);
    } catch (e) {
      checks.push({
        id: "json-parse",
        detail: `strict JSON parse failed: ${(e as Error).message}`,
        ok: false,
      });
      throw new AppError(
        "INVALID_JSON",
        `JSON block ${ev.cid} is not valid JSON: ${(e as Error).message}`,
        { cid: ev.cid }
      );
    }
    checks.push({
      id: "json-parse",
      detail:
        "RFC 8259 parse succeeded (duplicate keys and lone surrogate escapes rejected)",
      ok: true,
    });
    ev.jsonEncodingValid = true;

    try {
      const parsed = validateMetadataShape(value, isRoot);
      checks.push({
        id: "metadata-schema",
        detail: isRoot
          ? "schema ok: name/version/image/image_type/image_size validated, links recursively referenced"
          : "link schema ok: name/image/image_type/image_size validated (no version on links)",
        ok: true,
      });
      return parsed;
    } catch (e) {
      throw new AppError(
        "INVALID_METADATA",
        `metadata schema violation in ${ev.cid}: ${(e as Error).message}`,
        { cid: ev.cid }
      );
    }
  }
}

interface WalkCtx {
  stack: string[];
  seen: Map<string, NodeEvidence>;
  count: number;
  rootMetadata?: ParsedMetadata;
}

function codecName(code: number): "raw" | "json" | "dag-pb" {
  switch (code) {
    case CODEC_RAW:
      return "raw";
    case CODEC_JSON:
      return "json";
    default:
      return "dag-pb";
  }
}

export function parseCidOrThrow(text: string, field = "CID"): Cid {
  try {
    return parseCid(text);
  } catch (e) {
    throw new AppError("UNSUPPORTED_CID", `invalid CID at ${field}: ${(e as Error).message}`, {
      value: text,
    });
  }
}
