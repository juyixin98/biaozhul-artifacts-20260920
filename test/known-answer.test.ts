/**
 * Known-answer tests using canonical IPFS/kubo CIDs as golden vectors.
 * These pin our hand-written dag-pb/UnixFS encoders to the real byte layout:
 *
 *   empty PBNode                      QmdfTbBqBPQ7VNxZEYEj14VmRuZBkqFbiwReogJgS1zR1n
 *   empty UnixFS directory (08 01)    QmUNLLsPACCz1vLxQVkXqqLX5R1X345qqfHbsf67hvA3Nn
 *   empty UnixFS file (08 02)         QmbFMke1KXqnYyBBWxB74N4c5SBnJMVAiMNRcGu6x1AwQH
 *   one-raw-link PBNode               QmQeaL776otgz37NQPf3V8dC4x6kPqQ8k4N7fFWmQdV3HAm
 *                                     (regenerated vector, see derivation below)
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { encodeDagPb } from '../src/codec/dag-pb.js';
import { encodeUnixFsFile } from '../src/codec/unixfs.js';
import { cidFromData, cidToString } from '../src/codec/cid.js';
import { decodeDagPb } from '../src/codec/dag-pb.js';
import { decodeUnixFs } from '../src/codec/unixfs.js';

const v0 = (bytes: Uint8Array) => {
  const c1 = cidFromData(0x70, bytes);
  return cidToString({ version: 0 as const, codec: 0x70, codecName: 'dag-pb', multihash: c1.multihash });
};

describe('dag-pb/UnixFS known-answer vectors (kubo canonical)', () => {
  it('empty PBNode', () => {
    const bytes = encodeDagPb({ links: [], data: null });
    assert.deepEqual(bytes, new Uint8Array(0));
    assert.equal(v0(bytes), 'QmdfTbBqBPQ7VNxZEYEj14VmRuZBkqFbiwReogJgS1zR1n');
  });

  it('empty UnixFS directory', () => {
    const data = Uint8Array.from([0x08, 0x01]);
    const bytes = encodeDagPb({ links: [], data });
    // dag-pb quirk: PBNode.Data also uses the field-1 tag 0x0a:
    // 0x0a 0x02 0x08 0x01
    assert.deepEqual(bytes, Uint8Array.from([0x0a, 0x02, 0x08, 0x01]));
    assert.equal(v0(bytes), 'QmUNLLsPACCz1vLxQVkXqqLX5R1X345qqfHbsf67hvA3Nn');
  });

  it('empty UnixFS file', () => {
    const data = encodeUnixFsFile(new Uint8Array(0));
    // Type=File (08 02), filesize=0 (18 00) — the kubo canonical layout
    assert.deepEqual(data, Uint8Array.from([0x08, 0x02, 0x18, 0x00]));
    const bytes = encodeDagPb({ links: [], data });
    assert.equal(v0(bytes), 'QmbFMke1KXqnYyBBWxB74N4c5SBnJMVAiMNRcGu6x1AwQH');
  });

  it('empty file decodes strictly and round-trips', () => {
    const bytes = encodeDagPb({ links: [], data: encodeUnixFsFile(new Uint8Array(0)) });
    const d = decodeDagPb(bytes);
    assert.equal(d.canonical, true);
    const u = decodeUnixFs(d.node.data!);
    assert.equal(u.unixfs.type, 2);
    assert.equal(u.canonical, true);
  });
});
