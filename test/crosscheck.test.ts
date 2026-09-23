/**
 * Cross-validation against the independent `multiformats` reference library
 * (dev dependency only; production code has no multiformats dependency).
 * If our hand-written base/multihash/CID layers ever drift, this fails.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { CID as RefCID } from 'multiformats/cid';
import { sha256 as refSha256 } from 'multiformats/hashes/sha2';
import { parseCID, cidToString, cidFromData } from '../src/codec/cid.js';
import { base58btcDecode, base58btcEncode, base32Decode, base32LowerEncode } from '../src/codec/base.js';

describe('cross-check: multiformats reference interop', () => {
  const bundle = JSON.parse(
    readFileSync(join(process.cwd(), 'examples', 'blocks', 'all-blocks.json'), 'utf8'),
  ) as Record<string, string>;

  for (const [text, b64] of Object.entries(bundle)) {
    it(`parses and re-derives fixture CID ${text.slice(0, 24)}…`, async () => {
      const ref = RefCID.parse(text);
      const ours = parseCID(text);
      assert.equal(cidToString(ours), ref.toString());

      const bytes = new Uint8Array(Buffer.from(b64, 'base64'));
      const digest = await refSha256.digest(bytes);
      const rebuilt = RefCID.createV1(ours.codec, digest);
      const expectedText = ours.version === 0 ? rebuilt.toV0().toString() : rebuilt.toString();
      assert.equal(text, expectedText);
    });
  }

  it('raw codec CID matches reference', async () => {
    const data = new TextEncoder().encode('hello world');
    const ref = RefCID.createV1(0x55, await refSha256.digest(data)).toString();
    assert.equal(cidToString(cidFromData(0x55, data)), ref);
  });

  it('CIDv0 matches reference', async () => {
    const data = new TextEncoder().encode('hello world');
    const ref = RefCID.createV0(await refSha256.digest(data)).toString();
    const ours = cidToString({
      version: 0 as const,
      codec: 0x70,
      codecName: 'dag-pb',
      multihash: cidFromData(0x70, data).multihash,
    });
    assert.equal(ours, ref);
  });

  it('base58btc and base32 round-trip', () => {
    const vectors = [new Uint8Array([0, 0, 1, 255]), Uint8Array.from({ length: 256 }, (_, i) => i)];
    for (const v of vectors) {
      const e58 = base58btcEncode(v);
      assert.deepEqual(base58btcDecode(e58), v);
      const e32 = base32LowerEncode(v);
      assert.deepEqual(base32Decode(e32, { rejectUpperCase: true }), v);
    }
    assert.equal(base58btcEncode(new Uint8Array(0)), '');
  });
});
