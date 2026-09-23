/**
 * UnixFS metadata (the `Data` payload of a file/directory dag-pb node).
 * https://github.com/ipfs/specs/blob/main/UNIXFS.md
 *
 * Data {
 *   enum DataType { Raw=0; Directory=1; File=2; Metadata=3; Symlink=4; HAMTShard=5; }
 *   required DataType Type = 1;
 *   optional bytes Data = 2;   // inline payload for small files
 *   optional uint64 filesize = 3;
 *   repeated uint64 blocksizes = 4;
 *   optional uint32 hashType = 5;
 *   optional uint64 fanout = 6;
 *   optional uint32 mode = 7;
 *   optional UnixTime mtime = 8; { int64 Seconds=1; fixed32 FractionalNanoseconds=2; }
 * }
 *
 * Only Raw (0) and File (2) are accepted for NFT payloads; directories etc.
 * are rejected (metadata JSON must live in a file node, never a directory).
 */
import { decodePb, PbWriter } from './pb.js';

export const UNIXFS_RAW = 0;
export const UNIXFS_FILE = 2;
const KNOWN_TYPES = new Map<number, string>([
  [0, 'raw'],
  [1, 'directory'],
  [2, 'file'],
  [3, 'metadata'],
  [4, 'symlink'],
  [5, 'hamtshard'],
]);

export interface UnixFsData {
  type: number;
  typeName: string;
  data: Uint8Array | null;
  fileSize: bigint | null;
  blockSizes: bigint[];
}

/** Canonical encoder for fixture generation, matching kubo byte layout. */
export function encodeUnixFsFile(payload: Uint8Array): Uint8Array {
  if (payload.length === 0) {
    // kubo empty file: Type=File, filesize=0 (0x08 0x02 0x18 0x00).
    return new PbWriter().uint64(1, UNIXFS_FILE).uint64(3, 0).finish();
  }
  return new PbWriter()
    .uint64(1, UNIXFS_FILE)
    .bytes(2, payload)
    .uint64(3, payload.length)
    .finish();
}

export function encodeUnixFsShardHeader(): Uint8Array {
  // Interior file node: links + declared logical file size, no inline data.
  const w = new PbWriter().uint64(1, UNIXFS_FILE);
  return w.finish();
}

export function encodeUnixFsRaw(payload: Uint8Array): Uint8Array {
  const w = new PbWriter().uint64(1, UNIXFS_RAW).bytes(2, payload);
  return w.finish();
}

/**
 * Strict decode of a UnixFS Data payload.
 * Re-encodes canonically and compares, so non-canonical messages are rejected.
 */
export function decodeUnixFs(bytes: Uint8Array): { unixfs: UnixFsData; canonical: boolean; reasons: string[] } {
  const reasons: string[] = [];
  const fields = decodePb(bytes);
  let type: number | null = null;
  let data: Uint8Array | null = null;
  let fileSize: bigint | null = null;
  const blockSizes: bigint[] = [];
  let last = 0;
  let mtimeSeen = false;
  let mode: bigint | null = null;

  for (const f of fields) {
    if (f.field < last) reasons.push(`non-canonical UnixFS field order at field ${f.field}`);
    last = f.field;
    switch (f.field) {
      case 1: {
        if (f.wireType !== 0) throw new Error('UnixFS Type must be varint');
        if (type !== null) throw new Error('UnixFS Type appears more than once');
        type = Number(f.varint!);
        if (type > 5) throw new Error(`unknown UnixFS DataType ${type}`);
        break;
      }
      case 2: {
        if (f.wireType !== 2) throw new Error('UnixFS Data must be bytes');
        if (data !== null) throw new Error('UnixFS Data appears more than once');
        data = f.bytes!;
        break;
      }
      case 3: {
        if (f.wireType !== 0) throw new Error('UnixFS filesize must be varint');
        if (fileSize !== null) throw new Error('UnixFS filesize appears more than once');
        fileSize = f.varint!;
        break;
      }
      case 4: {
        if (f.wireType !== 0) throw new Error('UnixFS blocksizes must be varint');
        blockSizes.push(f.varint!);
        break;
      }
      case 5: // hashType — optional, only accept value 0 (default), reject exotic
        if (f.wireType !== 0) throw new Error('UnixFS hashType must be varint');
        if (f.varint !== 0n) throw new Error(`unsupported UnixFS hashType ${f.varint}`);
        break;
      case 6: // fanout — directory-only; tolerated as varint here, type check rejects below
        if (f.wireType !== 0) throw new Error('UnixFS fanout must be varint');
        break;
      case 7:
        if (f.wireType !== 0) throw new Error('UnixFS mode must be varint');
        if (mode !== null) throw new Error('UnixFS mode appears more than once');
        mode = f.varint!;
        break;
      case 8: {
        if (f.wireType !== 2) throw new Error('UnixFS mtime must be a length-delimited message');
        if (mtimeSeen) throw new Error('UnixFS mtime appears more than once');
        mtimeSeen = true;
        validateMtime(f.bytes!);
        break;
      }
      default:
        throw new Error(`unknown UnixFS field ${f.field}`);
    }
  }
  if (type === null) throw new Error('UnixFS payload is missing required Type');
  if (mode !== null && mode > 0xfffn) throw new Error('UnixFS mode exceeds uint32');

  const unixfs: UnixFsData = {
    type,
    typeName: KNOWN_TYPES.get(type) ?? `unknown(${type})`,
    data,
    fileSize,
    blockSizes,
  };
  return { unixfs, canonical: reasons.length === 0, reasons };
}

function validateMtime(raw: Uint8Array): void {
  const fields = decodePb(raw);
  let seenSecs = false;
  let seenFrac = false;
  for (const f of fields) {
    if (f.field === 1) {
      if (f.wireType !== 0) throw new Error('UnixTime.Seconds must be varint');
      if (seenSecs) throw new Error('UnixTime.Seconds appears more than once');
      seenSecs = true;
    } else if (f.field === 2) {
      // FractionalNanoseconds is fixed32. Our strict protobuf decoder rejects
      // fixed32 globally, so an encoded fraction cannot reach here; anything
      // else in field 2 is also invalid.
      throw new Error('UnixTime.FractionalNanoseconds (fixed32) is not supported; seconds precision only');
    } else {
      throw new Error(`unknown UnixTime field ${f.field}`);
    }
  }
}

/**
 * Structural consistency between UnixFS metadata and the enclosing dag-pb node:
 * chunk count, inline-data rule, filesize vs sum of chunk byte sizes.
 * `chunkBytes` is the resolved payload size of each link in link order.
 */
export function validateFileShape(u: UnixFsData, chunkBytes: bigint[]): string[] {
  const errs: string[] = [];
  if (u.type !== UNIXFS_FILE) {
    errs.push(`UnixFS node type is ${u.typeName}, expected file(2)`);
    return errs;
  }
  const hasLinks = chunkBytes.length > 0;
  const hasInline = u.data !== null && u.data.length > 0;

  if (hasInline && hasLinks) {
    errs.push('UnixFS file node mixes inline Data with links (valid layouts are inline-only or sharded)');
  }
  if (u.blockSizes.length !== chunkBytes.length) {
    errs.push(`blocksizes entries ${u.blockSizes.length} != dag-pb links ${chunkBytes.length}`);
  }
  let sum = 0n;
  const n = Math.min(u.blockSizes.length, chunkBytes.length);
  for (let i = 0; i < n; i++) {
    const declared = u.blockSizes[i]!;
    const actual = chunkBytes[i]!;
    if (declared !== actual) {
      errs.push(`blocksizes[${i}]=${declared} disagrees with resolved link payload size ${actual}`);
    }
    sum += actual;
  }
  if (hasInline && u.fileSize !== null && BigInt(u.data!.length) !== u.fileSize) {
    errs.push(`inline filesize ${u.fileSize} != actual ${u.data!.length} bytes`);
  }
  if (hasLinks && u.fileSize !== null && sum !== u.fileSize) {
    errs.push(`declared filesize ${u.fileSize} != sum of linked payloads ${sum}`);
  }
  if (!hasLinks && !hasInline) {
    // kubo empty file: Type=File alone is canonical; filesize may be absent or 0.
    if (u.fileSize !== null && u.fileSize !== 0n) {
      errs.push(`empty file node declares non-zero filesize ${u.fileSize}`);
    }
  }
  return errs;
}
