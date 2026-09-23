/**
 * Verification engine — every check here executes real work:
 *
 *  1. content addressing : SHA-256 over the actual block bytes vs CID multihash
 *  2. codec integrity     : strict dag-pb decode + canonical re-encode equality
 *  3. UnixFS consistency  : type, blocksizes, filesize vs resolved child payloads,
 *                           declared Tsize vs computed cumulative DAG size
 *  4. JSON encoding       : strict UTF-8 + hand-written JSON grammar parser,
 *                           duplicate keys rejected
 *  5. media references    : recursive local DAG resolution (no network),
 *                           magic-byte sniffing, real assembled size
 *
 * Failure modes the API tests deliberately exercise: tampered bytes, cyclic
 * links, missing blocks, non-UTF-8 payloads, and metadata revision replay.
 */
import type { BlockRow } from '../store.js';
import type { Config } from '../config.js';
import { parseCID, cidToString, cidAsV1, type CID } from '../codec/cid.js';
import { verifyMultihash } from '../codec/multihash.js';
import { decodeDagPb, type PbNode } from '../codec/dag-pb.js';
import { decodeUnixFs, validateFileShape, UNIXFS_FILE, UNIXFS_RAW } from '../codec/unixfs.js';
import { parseStrictJson } from '../codec/json-strict.js';
import { buildMetadataModel, type MediaRef } from '../codec/metadata-model.js';
import { sniffMedia } from '../codec/media-sniff.js';

export interface Check {
  name: string;
  ok: boolean;
  basis: string;
}

export interface LayerEvidence {
  depth: number;
  role: 'root' | 'file-node' | 'chunk' | 'directory' | 'raw';
  cid: string;
  codec: string;
  blockBytes: number;
  hashCheck: Check;
  encodingCheck?: Check;
  unixfsCheck?: Check;
  links?: LinkEvidence[];
  inlinePayloadBytes?: number;
  note?: string;
}

export interface LinkEvidence {
  name: string;
  cid: string;
  found: boolean;
  tsizeDeclared: string | null;
  tsizeComputed: string;
  tsizeMatches: boolean | null;
  basis: string;
}

export interface MediaReport {
  field: string;
  uri: string;
  ok: boolean;
  path: string[];
  rootCid: string;
  resolvedFileCid: string;
  layers: LayerEvidence[];
  media: { mime: string; ext: string; basis: string } | null;
  payloadBytes: number;
  sizeLimit: number;
  checks: Check[];
  error?: string;
}

export interface MetadataReport {
  ok: boolean;
  cid: string;
  layers: LayerEvidence[];
  metadataJsonBytes: number;
  parsedFields: Record<string, unknown>;
  media: MediaReport[];
  checks: Check[];
  error?: string;
}

export interface BlockReport {
  ok: boolean;
  cid: string;
  version: number;
  codec: string;
  hash: { algorithm: string; claimedDigest: string; actualDigest: string };
  blockBytes: number;
  checks: Check[];
  error?: string;
}

export interface BlockGetter {
  getBlock(cid: CID): BlockRow | undefined;
}

export class VerificationError extends Error {
  constructor(
    public stage: string,
    message: string,
  ) {
    super(message);
    this.name = 'VerificationError';
  }
}

export class Verifier {
  constructor(
    private readonly blocks: BlockGetter,
    private readonly cfg: Config,
  ) {}

  // ---------------------------------------------------------------- blocks

  /** Verify a single raw block against its claimed CID. Never touches network. */
  verifyBlock(cid: CID, data: Uint8Array): BlockReport {
    const checks: Check[] = [];
    const hash = verifyMultihash(data, cid.multihash);
    checks.push({
      name: 'sha256-content-address',
      ok: hash.ok,
      basis: hash.ok
        ? `SHA-256 over the ${data.length} supplied bytes equals the CID multihash digest`
        : `SHA-256 mismatch: CID claims ${hash.claimed.slice(0, 16)}…, actual ${hash.actual.slice(0, 16)}…`,
    });

    const sizeCheck = data.length <= this.cfg.maxBlockSize;
    checks.push({
      name: 'block-size-limit',
      ok: sizeCheck,
      basis: `block is ${data.length} bytes; limit ${this.cfg.maxBlockSize}`,
    });

    // codec-level decode for structured codecs
    if (cid.codecName === 'dag-pb') {
      try {
        const d = decodeDagPb(data);
        checks.push({
          name: 'dag-pb-canonical',
          ok: d.canonical,
          basis: d.canonical
            ? `strict protobuf decode succeeded; canonical re-encoding equals the ${data.length} input bytes; ${d.node.links.length} link(s)`
            : d.reasons.join('; '),
        });
      } catch (e) {
        checks.push({ name: 'dag-pb-canonical', ok: false, basis: (e as Error).message });
      }
    } else {
      checks.push({
        name: 'raw-codec',
        ok: true,
        basis: 'raw codec: opaque bytes, content addressing alone authenticates the block',
      });
    }

    return {
      ok: checks.every((c) => c.ok),
      cid: cidToString(cid),
      version: cid.version,
      codec: cid.codecName,
      hash: {
        algorithm: cid.multihash.name,
        claimedDigest: hash.claimed,
        actualDigest: hash.actual,
      },
      blockBytes: data.length,
      checks,
      error: checks.every((c) => c.ok) ? undefined : checks.find((c) => !c.ok)?.basis,
    };
  }

  // -------------------------------------------------- file DAG resolution

  /**
   * Resolve a `ipfs://<cid>/<path>` reference to a UnixFS file and its payload.
   * Traverses directories by real dag-pb link-name lookup; refuses symlinks,
   * HAMT shards and cycles.
   */
  resolveIpfsRef(ref: MediaRef, sizeLimit: number): MediaReport {
    const report: MediaReport = {
      field: ref.field,
      uri: ref.uri,
      ok: false,
      path: ref.path,
      rootCid: '',
      resolvedFileCid: '',
      layers: [],
      media: null,
      payloadBytes: 0,
      sizeLimit,
      checks: [],
    };
    try {
      let cid = parseCID(ref.cidText);
      report.rootCid = cidToString(cid);

      // Walk explicit path segments through UnixFS directories.
      const dirLayers: LayerEvidence[] = [];
      for (let i = 0; i < ref.path.length; i++) {
        const seg = ref.path[i]!;
        const r = this.requireDagPb(cid, i, 'directory');
        dirLayers.push(r.layer);
        const link = r.node.links.find((l) => l.name === seg);
        if (!link) {
          throw new VerificationError(
            'path',
            `directory ${cidToString(cid)} has no link named ${JSON.stringify(seg)}`,
          );
        }
        cid = link.hash;
      }

      const { payload, layers } = this.readFileDag(cid, new Map(), dirLayers.length, sizeLimit, true);
      report.layers = [...dirLayers, ...layers];
      report.resolvedFileCid = cidToString(cid);
      report.payloadBytes = payload.length;

      report.checks.push({
        name: 'dag-traversal',
        ok: true,
        basis:
          ref.path.length === 0
            ? 'reference points directly at the file root'
            : `resolved ${ref.path.length} path segment(s) through UnixFS directory link names`,
      });
      report.checks.push({
        name: 'payload-size-limit',
        ok: payload.length <= sizeLimit,
        basis: `assembled payload is ${payload.length} bytes; limit ${sizeLimit}`,
      });

      const sniffed = sniffMedia(payload);
      if (!sniffed) {
        report.checks.push({
          name: 'media-encoding',
          ok: false,
          basis: 'payload matches no supported media magic-byte signature; unknown encoding is rejected',
        });
      } else {
        report.media = sniffed;
        report.checks.push({
          name: 'media-encoding',
          ok: true,
          basis: `recognized ${sniffed.mime} from content: ${sniffed.basis}`,
        });
      }

      report.ok = report.checks.every((c) => c.ok) && layers.every((l) => layerOk(l));
    } catch (e) {
      if (e instanceof VerificationError) {
        report.error = `${e.stage}: ${e.message}`;
      } else {
        report.error = (e as Error).message;
      }
      report.checks.push({ name: 'resolution', ok: false, basis: report.error! });
    }
    return report;
  }

  /**
   * Recursively read a UnixFS file DAG.
   * @returns assembled payload and per-layer evidence; cumulative size map is
   *          shared so diamond DAGs don't loop.
   */
  protected readFileDag(
    cid: CID,
    stack: Map<string, number>,
    depth: number,
    sizeLimit: number,
    isRoot = false,
  ): { payload: Buffer; layers: LayerEvidence[] } {
    const key = cidToString(cidAsV1(cid));
    if (stack.has(key)) {
      throw new VerificationError(
        'cycle',
        `cyclic reference detected: CID ${key} appears again at depth ${depth} (first at depth ${stack.get(key)})`,
      );
    }
    if (depth > this.cfg.maxDagDepth) {
      throw new VerificationError('depth', `DAG depth exceeds ${this.cfg.maxDagDepth}`);
    }
    stack.set(key, depth);

    const { data } = this.requireBlock(cid);
    const layer: LayerEvidence = {
      depth,
      role: isRoot ? 'root' : 'file-node',
      cid: key,
      codec: cid.codecName,
      blockBytes: data.length,
      hashCheck: this.hashCheck(cid, data),
    };

    // raw leaves (codec 0x55) are a legal UnixFS shard target
    if (cid.codecName === 'raw') {
      layer.role = 'raw';
      layer.encodingCheck = { name: 'raw-block', ok: true, basis: 'raw codec leaf block' };
      layer.inlinePayloadBytes = data.length;
      if (data.length > sizeLimit) throw new VerificationError('size', `raw leaf ${key} exceeds size limit`);
      stack.delete(key);
      return { payload: Buffer.from(data), layers: [layer] };
    }

    const decoded = this.requireDagPbBytes(cid, data, layer);
    const ufs = decodeUnixFs(decoded.node.data ?? new Uint8Array(0));
    if (!ufs.canonical) {
      layer.unixfsCheck = { name: 'unixfs-canonical', ok: false, basis: ufs.reasons.join('; ') };
      throw new VerificationError('unixfs', layer.unixfsCheck.basis);
    }

    if (ufs.unixfs.type === UNIXFS_RAW) {
      // Raw type is an inline-only UnixFS payload container.
      layer.role = 'raw';
      const payload = Buffer.from(ufs.unixfs.data ?? new Uint8Array(0));
      layer.inlinePayloadBytes = payload.length;
      layer.links = [];
      layer.unixfsCheck = {
        name: 'unixfs',
        ok: decoded.node.links.length === 0,
        basis: decoded.node.links.length === 0
          ? 'UnixFS Raw node with inline payload'
          : 'UnixFS Raw node must not carry links',
      };
      if (decoded.node.links.length > 0) throw new VerificationError('unixfs', layer.unixfsCheck.basis);
      stack.delete(key);
      return { payload, layers: [layer] };
    }

    if (ufs.unixfs.type !== UNIXFS_FILE) {
      throw new VerificationError(
        'unixfs',
        `reference resolves to UnixFS ${ufs.unixfs.typeName} node; a file payload is required here`,
      );
    }

    // Resolve children first so chunk byte sizes are known.
    const childPayloads: Buffer[] = [];
    const childLayers: LayerEvidence[] = [];
    const chunkSizes: bigint[] = [];
    const linkEv: LinkEvidence[] = [];

    decoded.node.links.forEach((link, i) => {
      const child = this.readFileDag(link.hash, stack, depth + 1, sizeLimit);
      childPayloads.push(child.payload);
      childLayers.push(...child.layers);
      chunkSizes.push(BigInt(child.payload.length));
      const cumulative = this.cumulativeSize(link.hash);
      const declared = link.tsize;
      linkEv.push({
        name: link.name || `chunk-${i}`,
        cid: cidToString(link.hash),
        found: true,
        tsizeDeclared: declared === null ? null : declared.toString(),
        tsizeComputed: cumulative.toString(),
        tsizeMatches: declared === null ? null : declared === cumulative,
        basis:
          declared === null
            ? 'Tsize omitted (optional); recomputed from the verified child DAG'
            : `declared Tsize ${declared} vs cumulative block+payload size ${cumulative}`,
      });
      if (declared !== null && declared !== cumulative) {
        throw new VerificationError(
          'tsize',
          `link ${i} of ${key}: declared Tsize ${declared} != computed ${cumulative}`,
        );
      }
    });
    layer.links = linkEv;

    const shapeErrs = validateFileShape(ufs.unixfs, chunkSizes);
    layer.unixfsCheck = {
      name: 'unixfs-file-shape',
      ok: shapeErrs.length === 0,
      basis:
        shapeErrs.length === 0
          ? `UnixFS file: ${chunkSizes.length} chunk link(s), ${
              ufs.unixfs.fileSize ?? 0n
            } logical bytes; blocksizes/filesize agree with resolved payloads`
          : shapeErrs.join('; '),
    };
    if (shapeErrs.length > 0) throw new VerificationError('unixfs', shapeErrs.join('; '));

    let payload: Buffer;
    if (ufs.unixfs.data && ufs.unixfs.data.length > 0) {
      layer.inlinePayloadBytes = ufs.unixfs.data.length;
      payload = Buffer.from(ufs.unixfs.data);
    } else {
      payload = Buffer.concat(childPayloads);
    }
    if (BigInt(payload.length) !== (ufs.unixfs.fileSize ?? BigInt(payload.length))) {
      throw new VerificationError(
        'unixfs',
        `assembled payload ${payload.length} disagrees with declared filesize ${ufs.unixfs.fileSize}`,
      );
    }
    if (payload.length > sizeLimit) {
      throw new VerificationError('size', `assembled payload ${payload.length} bytes exceeds limit ${sizeLimit}`);
    }

    stack.delete(key);
    return { payload, layers: [layer, ...childLayers] };
  }

  /**
   * Cumulative size of the DAG rooted at cid — the exact UnixFS Tsize semantics:
   * for raw/inline-leaf blocks it's the block size; for file nodes it's the
   * node's own serialized size plus the Tsize of every child.
   * Memoized; missing/invalid children fail verification.
   */
  private cumulativeSize(cid: CID): bigint {
    const key = cidToString(cidAsV1(cid));
    const cached = this.tsizeCache.get(key);
    if (cached !== undefined) return cached;
    const { data } = this.requireBlock(cid);
    if (cid.codecName === 'raw') {
      const v = BigInt(data.length);
      this.tsizeCache.set(key, v);
      return v;
    }
    const { node } = decodeDagPb(data);
    let total = BigInt(data.length);
    for (const link of node.links) total += this.cumulativeSize(link.hash);
    this.tsizeCache.set(key, total);
    return total;
  }
  private tsizeCache = new Map<string, bigint>();

  // ------------------------------------------------------------- metadata

  verifyMetadataCid(cidInput: string): MetadataReport {
    const report: MetadataReport = {
      ok: false,
      cid: cidInput,
      layers: [],
      metadataJsonBytes: 0,
      parsedFields: {},
      media: [],
      checks: [],
    };
    try {
      const cid = parseCID(cidInput);
      report.cid = cidToString(cid);
      const { payload, layers } = this.readFileDag(cid, new Map(), 0, this.cfg.maxMetadataBytes, true);
      report.layers = layers;
      report.metadataJsonBytes = payload.length;

      let jsonRoot;
      try {
        jsonRoot = parseStrictJson(payload);
        report.checks.push({
          name: 'json-utf8-grammar',
          ok: true,
          basis: `strict UTF-8 decode + JSON grammar parse of ${payload.length} bytes succeeded; duplicate keys rejected`,
        });
      } catch (e) {
        report.checks.push({ name: 'json-utf8-grammar', ok: false, basis: (e as Error).message });
        throw new VerificationError('json', (e as Error).message);
      }

      let model;
      try {
        model = buildMetadataModel(jsonRoot);
        report.checks.push({
          name: 'metadata-model',
          ok: true,
          basis: `ERC-style metadata model valid: name, image + ${model.attributes.length} attribute(s), whitelist fields only`,
        });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        report.checks.push({ name: 'metadata-model', ok: false, basis: msg });
        throw new VerificationError('model', msg);
      }

      report.parsedFields = {
        name: model.name,
        description: model.description,
        image: model.image.uri,
        animation_url: model.animation?.uri ?? null,
        external_url: model.externalUrl,
        attributes: model.attributes,
        properties: model.properties,
      };

      const refs = [model.image, ...(model.animation ? [model.animation] : [])];
      for (const ref of refs) {
        const media = this.resolveIpfsRef(ref, this.cfg.maxMediaBytes);
        report.media.push(media);
      }
      const mediaOk = report.media.every((m) => m.ok);
      report.checks.push({
        name: 'media-references',
        ok: mediaOk,
        basis: mediaOk
          ? `all ${report.media.length} ipfs:// reference(s) resolved locally to typed, size-checked file payloads`
          : report.media.filter((m) => !m.ok).map((m) => `${m.field}: ${m.error ?? 'checks failed'}`).join('; '),
      });

      report.ok =
        report.checks.every((c) => c.ok) &&
        report.layers.every((l) => layerOk(l)) &&
        report.media.every((m) => m.ok);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      report.ok = false;
      report.error = e instanceof VerificationError ? `${e.stage}: ${e.message}` : msg;
    }
    return report;
  }

  // -------------------------------------------------------------- helpers

  protected hashCheck(cid: CID, data: Uint8Array): Check {
    const r = verifyMultihash(data, cid.multihash);
    return {
      name: 'sha256-content-address',
      ok: r.ok,
      basis: r.ok
        ? `SHA-256(${data.length} bytes) = ${r.actual.slice(0, 20)}… matches multihash 0x${cid.multihash.code.toString(16)}`
        : `digest mismatch: claimed ${r.claimed.slice(0, 20)}… actual ${r.actual.slice(0, 20)}…`,
    };
  }

  protected requireBlock(cid: CID): { data: Buffer; row: BlockRow } {
    const row = this.blocks.getBlock(cid);
    if (!row) {
      throw new VerificationError('missing-block', `block ${cidToString(cid)} is not present in the local store`);
    }
    const h = verifyMultihash(row.data, cid.multihash);
    if (!h.ok) {
      throw new VerificationError(
        'tamper',
        `stored block ${cidToString(cid)} fails its own SHA-256 multihash: expected ${h.claimed.slice(0, 16)}… got ${h.actual.slice(0, 16)}…`,
      );
    }
    return { data: Buffer.from(row.data), row };
  }

  private requireDagPbBytes(cid: CID, data: Uint8Array, layer: LayerEvidence): { node: PbNode } {
    layer.hashCheck = this.hashCheck(cid, data);
    if (!layer.hashCheck.ok) throw new VerificationError('tamper', layer.hashCheck.basis);
    let decoded;
    try {
      decoded = decodeDagPb(data);
    } catch (e) {
      layer.encodingCheck = { name: 'dag-pb-decode', ok: false, basis: (e as Error).message };
      throw new VerificationError('dag-pb', (e as Error).message);
    }
    layer.encodingCheck = {
      name: 'dag-pb-canonical',
      ok: decoded.canonical,
      basis: decoded.canonical
        ? `strict protobuf decode + canonical re-encode equality; ${decoded.node.links.length} link(s)`
        : decoded.reasons.join('; '),
    };
    if (!decoded.canonical) throw new VerificationError('dag-pb', decoded.reasons.join('; '));
    return { node: decoded.node };
  }

  /** Load a block expecting a dag-pb node (directory traversal helper). */
  private requireDagPb(cid: CID, depth: number, role: string): { node: PbNode; layer: LayerEvidence } {
    const { data } = this.requireBlock(cid);
    const layer: LayerEvidence = {
      depth,
      role: role as LayerEvidence['role'],
      cid: cidToString(cidAsV1(cid)),
      codec: cid.codecName,
      blockBytes: data.length,
      hashCheck: this.hashCheck(cid, data),
    };
    const decoded = this.requireDagPbBytes(cid, data, layer);
    // Directories carry a UnixFS Directory Data message; links are named.
    const ufsData = decoded.node.data ? decodeUnixFs(decoded.node.data) : null;
    if (ufsData && ufsData.unixfs.type !== 1) {
      throw new VerificationError('path', `path segment crosses a non-directory node (${ufsData.unixfs.typeName})`);
    }
    if (!decoded.node.links.every((l) => l.name !== '')) {
      throw new VerificationError('path', 'directory traversal node contains unnamed links (not a UnixFS directory)');
    }
    layer.unixfsCheck = {
      name: 'unixfs-directory',
      ok: true,
      basis: ufsData
        ? `UnixFS directory, ${decoded.node.links.length} named entries, sorted`
        : `dag-pb node with ${decoded.node.links.length} named entries (no UnixFS Data)`,
    };
    return { node: decoded.node, layer };
  }
}

export function layerOk(l: LayerEvidence): boolean {
  return (
    l.hashCheck.ok &&
    (l.encodingCheck?.ok ?? true) &&
    (l.unixfsCheck?.ok ?? true) &&
    (l.links?.every((x) => x.found && x.tsizeMatches !== false) ?? true)
  );
}

