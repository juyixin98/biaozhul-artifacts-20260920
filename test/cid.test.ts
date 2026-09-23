import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { parseCID, cidToString, cidFromData } from '../src/codec/cid.js';
import { base58btcDecode, base58btcEncode, base32Decode, base32LowerEncode } from '../src/codec/base.js';
import { decodeMultihash } from '../src/codec/multihash.js';

describe('CID parsing — strict whitelist', () => {
  it('accepts CIDv0 base58btc sha256 dag-pb (34 bytes)', () => {
    const c = parseCID('QmUNLLsPACCz1vLxQVkXqqLX5R1X345qqfHbsf67hvA3Nn');
    assert.equal(c.version, 0);
    assert.equal(c.codecName, 'dag-pb');
    assert.equal(c.multihash.name, 'sha2-256');
  });

  it('accepts CIDv1 base32 dag-pb and raw', () => {
    const data = new Uint8Array([1, 2, 3]);
    for (const codec of [0x70, 0x55] as const) {
      const text = cidToString(cidFromData(codec, data));
      const c = parseCID(text);
      assert.equal(c.version, 1);
      assert.ok(text.startsWith('b'));
    }
  });

  it('round-trips v0<->v1 textual forms with identical digest', () => {
    const c0 = parseCID('QmUNLLsPACCz1vLxQVkXqqLX5R1X345qqfHbsf67hvA3Nn');
    assert.equal(
      cidToString({ ...c0, version: 1 as const }),
      'bafybeiczsscdsbs7ffqz55asqdf3smv6klcw3gofszvwlyarci47bgf354',
    );
  });

  it('rejects non-b multibase encodings (z base58, m base64, etc.) for CIDv1', () => {
    // same 36-byte CIDv1 payload under a different multibase letter
    const b32 = cidToString(cidFromData(0x55, new Uint8Array([1]))).slice(1);
    // re-encode bytes in base58btc and prefix with 'z'
    const bytes = base32Decode(b32, { rejectUpperCase: false });
    assert.throws(() => parseCID('z' + base58btcEncode(bytes)), /unsupported multibase encoding/);
  });

  it('rejects CID version 2', () => {
    // valid base32 of bytes starting with multibase b + varint version 2
    const bytes = Uint8Array.from([2, 0x70, 0x12, 0x20, ...new Uint8Array(32)]);
    assert.throws(() => parseCID('b' + base32LowerEncode(bytes)), /unsupported CID version 2/);
  });

  it('rejects non-sha256 multihash algorithms (identity 0x00, sha1 0x11, sha512 0x13)', () => {
    const mk = (code: number, len: number) =>
      'b' + base32LowerEncode(Uint8Array.from([1, 0x55, code, len, ...new Uint8Array(len)]));
    assert.throws(() => parseCID(mk(0x00, 3)), /unsupported multihash code=0x0/);
    assert.throws(() => parseCID(mk(0x11, 20)), /unsupported multihash code=0x11/);
    assert.throws(() => parseCID(mk(0x13, 64)), /unsupported multihash code=0x13/);
  });

  it('rejects sha256 with a declared length other than 32', () => {
    const bytes = Uint8Array.from([1, 0x55, 0x12, 31, ...new Uint8Array(31)]);
    assert.throws(() => parseCID('b' + base32LowerEncode(bytes)), /only sha2-256 .* length 32/);
  });

  it('rejects unsupported CID codecs (dag-cbor 0x71)', () => {
    const bytes = Uint8Array.from([1, 0x71, 0x12, 0x20, ...new Uint8Array(32)]);
    assert.throws(() => parseCID('b' + base32LowerEncode(bytes)), /unsupported CID codec 0x71/);
  });

  it('rejects malformed base58 (invalid chars, wrong 0x12 0x20 shape)', () => {
    assert.throws(() => base58btcDecode('0OIl'), /invalid base58btc character/);
    // valid base58 chars but 34-byte decode with wrong prefix
    assert.throws(
      () => parseCID('QmNQuUgJFiJx6P5w'),
      /CIDv0 must decode to exactly 34 bytes|multihash|invalid/,
    );
  });

  it('rejects uppercase base32 under lowercase multibase b', () => {
    const b32 = cidToString(cidFromData(0x55, new Uint8Array([1]))).slice(1).toUpperCase();
    assert.throws(() => parseCID('b' + b32), /uppercase base32/);
  });

  it('rejects multihash length that does not match trailing bytes', () => {
    assert.throws(
      () => decodeMultihash(Uint8Array.from([0x12, 0x20, ...new Uint8Array(10)])),
      /multihash length 32 does not match/,
    );
  });
});
