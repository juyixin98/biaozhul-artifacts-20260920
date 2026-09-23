import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { encodeDagPb, decodeDagPb } from '../src/codec/dag-pb.js';
import { cidFromData, cidToString, cidToLinkBytes } from '../src/codec/cid.js';
import { decodePb } from '../src/codec/pb.js';
import { encodeUnixFsFile } from '../src/codec/unixfs.js';

const tag = (field: number, wire: number) => (field << 3) | wire;
const leaf = cidFromData(0x55, new Uint8Array([9, 9, 9]));
const HB = cidToLinkBytes(leaf);
const utf8 = (s: string) => new TextEncoder().encode(s);

/** Wrap PBLink fields into a PBNode 0x0a entry. */
function linkNode(inner: Uint8Array): Uint8Array {
  return Uint8Array.from([tag(1, 2), inner.length, ...inner]);
}
function linkFields(...parts: Uint8Array[]): Uint8Array {
  const inner = concat(parts);
  return linkNode(inner);
}
const fieldBytes = (field: number, b: Uint8Array) =>
  Uint8Array.from([tag(field, 2), b.length, ...b]);
const concat = (parts: Uint8Array[]) => {
  let n = 0;
  for (const p of parts) n += p.length;
  const out = new Uint8Array(n);
  let o = 0;
  for (const p of parts) {
    out.set(p, o);
    o += p.length;
  }
  return out;
};

describe('protobuf strict decoder', () => {
  it('rejects deprecated groups (wire 3 and 4)', () => {
    assert.throws(() => decodePb(Uint8Array.from([tag(1, 3), 0x7b])), /groups/i);
    assert.throws(() => decodePb(Uint8Array.from([tag(1, 4)])), /groups/i);
  });

  it('rejects unknown wire types 6 and 7', () => {
    assert.throws(() => decodePb(Uint8Array.from([tag(1, 6)])), /unknown wire type 6/);
    assert.throws(() => decodePb(Uint8Array.from([tag(1, 7)])), /unknown wire type 7/);
  });

  it('rejects field number 0 and non-minimal varints', () => {
    assert.throws(() => decodePb(Uint8Array.from([0, 0])), /tag zero|field number 0/i);
    assert.throws(() => decodePb(Uint8Array.from([tag(2, 0), 0x80, 0x00])), /non-minimal varint/);
  });

  it('rejects truncated length-delimited fields and truncated varints', () => {
    assert.throws(() => decodePb(Uint8Array.from([tag(2, 2), 5, 1, 2, 3])), /exceeds message|truncated/i);
    assert.throws(() => decodePb(Uint8Array.from([tag(1, 0), 0x80])), /truncated/i);
  });

  it('rejects fixed64/fixed32 wire types', () => {
    assert.throws(() => decodePb(Uint8Array.from([tag(1, 1), ...new Uint8Array(8)])), /fixed64/i);
    assert.throws(() => decodePb(Uint8Array.from([tag(1, 5), ...new Uint8Array(4)])), /fixed32/i);
  });
});

describe('dag-pb canonical validation', () => {
  it('encodes a canonical node and re-decodes it', () => {
    const node = {
      links: [
        { hash: leaf, name: 'a', tsize: 3n },
        { hash: leaf, name: 'b', tsize: 3n },
      ],
      data: encodeUnixFsFile(utf8('x')),
    };
    const bytes = encodeDagPb(node);
    const d = decodeDagPb(bytes);
    assert.equal(d.canonical, true, d.reasons.join('; '));
    assert.equal(d.node.links.length, 2);
    assert.deepEqual(d.node.links.map((l) => l.name), ['a', 'b']);
  });

  it('rejects unknown protobuf field numbers on PBNode and PBLink', () => {
    assert.throws(() => decodeDagPb(Uint8Array.from([tag(9, 2), 0])), /unknown PBNode/);
    // A link that carries a real Hash plus an unknown field 9 is rejected.
    const badLink = concat([fieldBytes(1, HB), Uint8Array.from([tag(9, 2), 0])]);
    assert.throws(() => decodeDagPb(linkNode(badLink)), /unknown PBLink field 9/);
  });

  it('rejects duplicated singular fields (Hash, Name)', () => {
    const dupHash = concat([fieldBytes(1, HB), fieldBytes(1, HB)]);
    assert.throws(() => decodeDagPb(linkNode(dupHash)), /Hash appears more than once/);

    const dupName = concat([fieldBytes(1, HB), fieldBytes(2, utf8('x')), fieldBytes(2, utf8('y'))]);
    assert.throws(() => decodeDagPb(linkNode(dupName)), /Name appears more than once/);
  });

  it('rejects a Data-like 0x0a entry appearing before the last position', () => {
    // Two UnixFS-Data-like entries (08 02): Data may only be the final 0x0a.
    const twoData = Uint8Array.from([tag(1, 2), 2, 0x08, 0x02, tag(1, 2), 2, 0x08, 0x02]);
    assert.throws(() => decodeDagPb(twoData), /Data may only appear after all links/);
  });

  it('flags unsorted and duplicate link names as non-canonical', () => {
    const enc = (names: string[]) => {
      const entries = names.map((n) =>
        concat([fieldBytes(1, HB), fieldBytes(2, utf8(n))]),
      );
      return concat(entries.map((inner) => linkNode(inner)));
    };
    assert.equal(decodeDagPb(enc(['b', 'a'])).canonical, false);
    assert.equal(decodeDagPb(enc(['a', 'a'])).canonical, false);
  });

  it('rejects non-UTF-8 link names', () => {
    const badName = Uint8Array.from([0xff, 0xfe]);
    const node = linkFields(fieldBytes(1, HB), fieldBytes(2, badName));
    assert.throws(() => decodeDagPb(node), /not valid UTF-8/);
  });

  it('treats an empty 0x0a payload as Data (not as a hashless link)', () => {
    const d = decodeDagPb(Uint8Array.from([tag(1, 2), 0]));
    assert.equal(d.node.links.length, 0);
    assert.deepEqual(d.node.data, new Uint8Array(0));
  });

  it('reports non-canonical for a link whose Name precedes Hash', () => {
    const inner = concat([fieldBytes(2, utf8('x')), fieldBytes(1, HB)]);
    const d = decodeDagPb(linkNode(inner));
    assert.equal(d.canonical, false);
    assert.match(d.reasons.join('; '), /field order|canonical/);
  });

  it('produces the canonical empty-directory CID', () => {
    const bytes = encodeDagPb({ links: [], data: Uint8Array.from([8, 1]) });
    const c0 = cidToString({
      version: 0 as const,
      codec: 0x70,
      codecName: 'dag-pb',
      multihash: cidFromData(0x70, bytes).multihash,
    });
    assert.equal(c0, 'QmUNLLsPACCz1vLxQVkXqqLX5R1X345qqfHbsf67hvA3Nn');
  });
});
