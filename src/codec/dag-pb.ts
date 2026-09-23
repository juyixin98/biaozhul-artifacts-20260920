/**
 * IPLD dag-pb codec — https://ipld.io/specs/codecs/dag-pb/spec/
 *
 * Historical protobuf quirk: BOTH PBNode fields use protobuf field number 1:
 *
 * PBNode { repeated PBLink Links = 1;   // tag 0x0a (field 1, wire type 2)
 *          optional bytes  Data  = 2; } // encoded tag is ALSO 0x0a (!)
 * PBLink { optional bytes  Hash  = 1;   // tag 0x0a
 *          optional string Name  = 2;   // tag 0x12
 *          optional uint64 Tsize = 3; } // tag 0x18
 *
 * i.e. Links and Data are distinguished on the wire by ordering: every 0x0a
 * message that parses as a PBLink is a Link; the trailing 0x0a bytes value is
 * Data. This decoder applies the dag-pb semantic model on top of the raw tags.
 *
 * Canonical rules enforced on BOTH encode and decode:
 *  - all Link entries precede Data
 *  - every link carries a CID in Hash (v0: bare 34-byte multihash; v1: full CID)
 *  - link names are strictly sorted byte-wise (UTF-8), and unique when present
 *  - no unknown fields, no duplicates of singular fields, no wrong wire types
 *  - Name is strict UTF-8 (decoder is fatal; surrogates/CESU-8 rejected)
 */
import { decodePb, PbWriter } from './pb.js';
import { cidFromLinkBytes, cidToLinkBytes, cidToString, type CID } from './cid.js';

export interface PbLink {
  hash: CID;
  name: string; // '' == omitted
  tsize: bigint | null;
}

export interface PbNode {
  links: PbLink[];
  data: Uint8Array | null;
}

const UTF8 = new TextEncoder();
const UTF8_FATAL = new TextDecoder('utf-8', { fatal: true });

function compareBytes(a: Uint8Array, b: Uint8Array): number {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    if (a[i] !== b[i]) return a[i]! - b[i]!;
  }
  return a.length - b.length;
}

/** Canonical encoder used by the fixture generator. */
export function encodeDagPb(node: PbNode): Uint8Array {
  const named = node.links.some((l) => l.name !== '');
  const ordered = [...node.links];
  if (named) {
    ordered.sort((a, b) => compareBytes(UTF8.encode(a.name), UTF8.encode(b.name)));
  }
  const w = new PbWriter();
  for (const link of ordered) {
    const inner = new PbWriter();
    inner.bytes(1, cidToLinkBytes(link.hash));
    if (link.name !== '') inner.bytes(2, UTF8.encode(link.name));
    if (link.tsize !== null && link.tsize > 0n) inner.uint64(3, link.tsize);
    // Links: tag 0x0a (field 1, wire 2)
    w.raw(wrapTag2(1, inner.finish()));
  }
  // Data: ALSO tag 0x0a (the dag-pb Data field encodes as field 1 on the wire)
  if (node.data !== null) w.raw(wrapTag2(1, node.data));
  return w.finish();
}

/** Encode <tag field=field wire=2><varint length><bytes>. */
function wrapTag2(field: number, value: Uint8Array): Uint8Array {
  const head: number[] = [(field << 3) | 2];
  let len = value.length;
  const lenBytes: number[] = [];
  do {
    const b = len & 0x7f;
    len >>>= 7;
    lenBytes.push(len ? b | 0x80 : b);
  } while (len);
  return Uint8Array.from([...head, ...lenBytes, ...value]);
}

class StrictError extends Error {}

/**
 * Strict decoder: returns the node and canonicalization evidence.
 * `canonical` is false when the block is parseable protobuf but violates a
 * dag-pb canonical rule — such a block can never legitimately carry a CID
 * (the CID commits to the canonical bytes), so callers treat it as invalid.
 */
export function decodeDagPb(bytes: Uint8Array): { node: PbNode; canonical: boolean; reasons: string[] } {
  const reasons: string[] = [];
  const fields = decodePb(bytes);
  // In dag-pb every PBNode field is wire-tagged 0x0a; collect them, then
  // decide Link vs Data by position: a 0x0a that cannot parse as a PBLink is
  // Data only when it is the FINAL such entry (matches @ipld/dag-pb).
  const entries = fields.filter((f) => f.field === 1 && f.wireType === 2);
  if (fields.some((f) => f.field !== 1 || f.wireType !== 2)) {
    const bad = fields.find((f) => f.field !== 1 || f.wireType !== 2)!;
    throw new StrictError(
      `unknown PBNode encoding: field ${bad.field} wire type ${bad.wireType}; dag-pb nodes use only 0x0a length-delimited entries`,
    );
  }

  const links: PbLink[] = [];
  let data: Uint8Array | null = null;

  entries.forEach((f, idx) => {
    const isLast = idx === entries.length - 1;
    const sub = decodePbOrNull(f.bytes!);
    // A PBLink is identified by a bytes-typed field 1 (Hash); a UnixFS Data
    // message uses a varint field 1 (Type). That wire-type difference is the
    // on-wire discriminator between a link and the trailing Data.
    const looksLikeLink = sub !== null && sub.some((sf) => sf.field === 1 && sf.wireType === 2);
    if (looksLikeLink) {
      links.push(decodeLinkFields(sub!, reasons)!);
      return;
    }
    // Not a link: this is Data, which may only be the final entry.
    if (!isLast) {
      throw new StrictError(
        `PBNode 0x0a entry #${idx} is not a PBLink and Data may only appear after all links`,
      );
    }
    data = f.bytes!;
  });

  // Name ordering / uniqueness (file chunk links all carry empty names).
  const named = links.some((l) => l.name !== '');
  if (named) {
    for (let i = 1; i < links.length; i++) {
      const cmp = compareBytes(UTF8.encode(links[i - 1]!.name), UTF8.encode(links[i]!.name));
      if (cmp === 0) {
        reasons.push(`duplicate link name ${JSON.stringify(links[i]!.name)}`);
      } else if (cmp > 0) {
        reasons.push(`link names are not sorted (${JSON.stringify(links[i - 1]!.name)} before ${JSON.stringify(links[i]!.name)})`);
      }
    }
  }

  const reEncoded = encodeDagPb({ links, data });
  if (reEncoded.length !== bytes.length || !Buffer.from(reEncoded).equals(Buffer.from(bytes))) {
    reasons.push('block bytes are not the canonical dag-pb encoding');
  }

  return { node: { links, data }, canonical: reasons.length === 0, reasons };
}

function decodePbOrNull(raw: Uint8Array): ReturnType<typeof decodePb> | null {
  try {
    return decodePb(raw);
  } catch {
    return null;
  }
}

/**
 * Strict PBLink decode. Returns null when the message could be node Data
 * instead (no fields / no Hash). Throws StrictError when it looks like a link
 * but violates PBLink rules (duplicate fields, bad UTF-8, unknown fields).
 * Pushes ordering observations into `reasons` for confirmed links.
 */
function decodeLinkFields(
  sub: ReturnType<typeof decodePb>,
  reasons: string[],
): PbLink | null {
  let hash: CID | null = null;
  let name: string | null = null;
  let tsize: bigint | null = null;
  let last = 0;
  let sawOther = false;
  for (const sf of sub) {
    if (sf.field < last) reasons.push('non-canonical PBLink field order');
    last = sf.field;
    if (sf.field === 1) {
      if (sf.wireType !== 2) throw new StrictError('PBLink.Hash must be bytes');
      if (hash) throw new StrictError('PBLink.Hash appears more than once');
      hash = cidFromLinkBytes(sf.bytes!);
    } else if (sf.field === 2) {
      if (sf.wireType !== 2) throw new StrictError('PBLink.Name must be bytes');
      if (name !== null) throw new StrictError('PBLink.Name appears more than once');
      try {
        name = UTF8_FATAL.decode(sf.bytes!);
      } catch {
        throw new StrictError('PBLink.Name is not valid UTF-8');
      }
      sawOther = true;
    } else if (sf.field === 3) {
      if (sf.wireType !== 0) throw new StrictError('PBLink.Tsize must be varint');
      if (tsize !== null) throw new StrictError('PBLink.Tsize appears more than once');
      tsize = sf.varint!;
      sawOther = true;
    } else {
      throw new StrictError(`unknown PBLink field ${sf.field}`);
    }
  }
  if (!hash) {
    // A message carrying Name/Tsize but no Hash is a malformed link, not Data.
    if (sawOther) throw new StrictError('PBLink is missing required Hash');
    return null;
  }
  return { hash, name: name ?? '', tsize };
}

export function linkDebugString(link: PbLink): string {
  return `${cidToString(link.hash)}${link.name ? ` (${link.name})` : ''}`;
}
