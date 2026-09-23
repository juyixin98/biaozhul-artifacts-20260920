/**
 * End-to-end adversarial tests driven through the real HTTP API and the
 * verification engine:
 *
 *  - tampered bytes (ingest-time SHA-256 mismatch; stored-block tamper)
 *  - cyclic references (via a forged block store that relabels real blocks)
 *  - missing blocks, non-UTF-8 payloads, duplicate JSON keys
 *  - unknown media encodings and non-ipfs (gateway) references
 *  - size limits, Tsize / blocksizes inconsistencies
 *  - metadata revision history, confirmation windows, chain reorg, replays
 */
import { describe, it, before } from 'node:test';
import assert from 'node:assert/strict';
import { makeHarness, buildSimpleNft, PNG_1PX, fakeMp4, type Harness } from './harness.js';
import { cidFromData, cidToString, parseCID, type CID } from '../src/codec/cid.js';
import { encodeDagPb, type PbLink } from '../src/codec/dag-pb.js';
import { encodeUnixFsFile } from '../src/codec/unixfs.js';
import { Verifier, type BlockGetter, type Check, type LayerEvidence } from '../src/verify/verifier.js';
import type { MetadataReport } from '../src/verify/verifier.js';
import type { BlockRow } from '../src/store.js';
import type { Config } from '../src/config.js';

let h: Harness;
before(async () => {
  h = await makeHarness();
});

const verifyMeta = async (app: Harness['app'], cid: string) => {
  const res = await app.inject({ method: 'POST', url: '/api/v1/verify/metadata', payload: { cid } });
  return { status: res.statusCode, body: JSON.parse(res.payload) as { ok: boolean; report: MetadataReport } };
};

/** Encode a protobuf length-independent varint field (for UnixFS messages). */
function varintField(field: number, value: bigint): Buffer {
  const out = [field << 3];
  let v = value;
  do {
    const byte = Number(v & 0x7fn);
    v >>= 7n;
    out.push(v > 0n ? byte | 0x80 : byte);
  } while (v > 0n);
  return Buffer.from(out);
}

function fileUnixfs(payloadSize: number, chunkSizes: number[]): Uint8Array {
  const parts = [Buffer.from([0x08, 0x02])]; // Type=File
  parts.push(varintField(3, BigInt(payloadSize)));
  for (const s of chunkSizes) parts.push(varintField(4, BigInt(s)));
  return new Uint8Array(Buffer.concat(parts));
}

// ---------------------------------------------------------------- happy path

describe('happy path', () => {
  it('verifies a complete metadata DAG and shows per-layer evidence', async () => {
    const built = await buildSimpleNft(h);
    const { status, body } = await verifyMeta(h.app, built.metadataCid);
    assert.equal(status, 200);
    assert.equal(body.ok, true);
    // metadata file layer + the media file layer reported under media[].layers
    assert.ok(body.report.layers.length >= 1);
    const img0 = body.report.media.find((m) => m.field === 'image')!;
    assert.ok(img0.layers.length >= 1);
    for (const layer of [...body.report.layers, ...img0.layers]) {
      assert.equal(layer.hashCheck.ok, true, layer.hashCheck.basis);
      assert.equal(layer.encodingCheck?.ok ?? true, true, layer.encodingCheck?.basis);
    }
    const img = body.report.media.find((m) => m.field === 'image')!;
    assert.equal(img.ok, true);
    assert.equal(img.media?.mime, 'image/png');
    assert.match(img.media!.basis, /89504e470d0a1a0a/);
    assert.equal(img.payloadBytes, PNG_1PX.length);
    assert.ok(body.report.checks.some((c) => c.name === 'json-utf8-grammar' && c.ok));
    assert.ok(body.report.checks.some((c) => c.name === 'metadata-model' && c.ok));
  });

  it('verifies a sharded media file: raw leaves, filesize, blocksizes and Tsize', async () => {
    const mp4 = fakeMp4(64);
    const mid = Math.ceil(mp4.length / 2);
    const a = mp4.subarray(0, mid);
    const b = mp4.subarray(mid);
    const ra = await h.ingest(a, 0x55);
    const rb = await h.ingest(b, 0x55);

    const links: PbLink[] = [
      { hash: parseCID(ra.cid), name: '', tsize: BigInt(a.length) },
      { hash: parseCID(rb.cid), name: '', tsize: BigInt(b.length) },
    ];
    const fileNode = encodeDagPb({ links, data: fileUnixfs(mp4.length, [a.length, b.length]) });
    const file = await h.ingest(fileNode);

    const metaJson = Buffer.from(JSON.stringify({ name: 'Sharded', image: `ipfs://${file.cid}` }), 'utf-8');
    const metaNode = encodeDagPb({ links: [], data: encodeUnixFsFile(metaJson) });
    const meta = await h.ingest(metaNode);
    const { body } = await verifyMeta(h.app, meta.cid);
    assert.equal(body.ok, true, JSON.stringify(body.report.error ?? body.report.checks, null, 2));
    const img = body.report.media[0]!;
    assert.equal(img.media?.mime, 'video/mp4');
    assert.equal(img.payloadBytes, mp4.length);
    const root = img.layers.find((l) => l.role === 'root')!;
    assert.equal(root.links?.length, 2);
    assert.ok(root.links!.every((l) => l.tsizeMatches === true));
  });

  it('resolves an image through a UnixFS directory path (named links)', async () => {
    const imgNode = encodeDagPb({ links: [], data: encodeUnixFsFile(PNG_1PX) });
    const img = await h.ingest(imgNode);
    // directory node: UnixFS Data Type=Directory(1)
    const dirNode = encodeDagPb({
      links: [{ hash: parseCID(img.cid), name: 'image.png', tsize: BigInt(imgNode.length) }],
      data: Uint8Array.from([0x08, 0x01]),
    });
    const dir = await h.ingest(dirNode);

    const metaJson = Buffer.from(
      JSON.stringify({ name: 'Dir', image: `ipfs://${dir.cid}/image.png` }),
      'utf-8',
    );
    const metaNode = encodeDagPb({ links: [], data: encodeUnixFsFile(metaJson) });
    const meta = await h.ingest(metaNode);
    const { body } = await verifyMeta(h.app, meta.cid);
    assert.equal(body.ok, true, body.report.error);
    const layers = body.report.media[0]!.layers;
    assert.equal(layers[0]!.role, 'directory');
    assert.equal(body.report.media[0]!.resolvedFileCid, img.cid);
  });
});

// ------------------------------------------------------------- tampering

describe('content addressing — tampering', () => {
  it('rejects byte-flipped block submission with real SHA-256 evidence', async () => {
    const built = await buildSimpleNft(h);
    const row = h.store.getBlock(parseCID(built.imageCid))!;
    const corrupt = Buffer.from(row.data);
    corrupt[10]! ^= 0xff;
    const res = await h.app.inject({
      method: 'POST',
      url: '/api/v1/blocks',
      payload: { cid: built.imageCid, dataBase64: corrupt.toString('base64') },
    });
    assert.equal(res.statusCode, 422);
    const body = JSON.parse(res.payload);
    assert.equal(body.report.checks[0].name, 'sha256-content-address');
    assert.equal(body.report.checks[0].ok, false);
    assert.match(body.report.checks[0].basis, /SHA-256 mismatch/);
  });

  it('fails metadata verification when a referenced block is missing', async () => {
    const built = await buildSimpleNft(h);
    const h2 = await makeHarness();
    const res = await h2.app.inject({
      method: 'POST',
      url: '/api/v1/verify/metadata',
      payload: { cid: built.metadataCid },
    });
    assert.equal(res.statusCode, 422);
    const body = JSON.parse(res.payload);
    assert.equal(body.ok, false);
    assert.match(JSON.stringify(body.report), /missing-block|not present/);
    await h2.close();
  });

  it('detects tamper of an already-stored block (engine-level forged getter)', async () => {
    const built = await buildSimpleNft(h);
    const good = h.store.getBlock(parseCID(built.imageCid))!;
    const corrupt = Buffer.from(good.data);
    corrupt[5]! ^= 0x01;
    const forged: BlockGetter = {
      getBlock(cid: CID): BlockRow | undefined {
        const cidText = cidToString(cid);
        if (cidText === built.imageCid) return { ...good, data: corrupt };
        return h.store.getBlock(cid);
      },
    };
    const v = new Verifier(forged, h.services.config);
    const report = v.verifyMetadataCid(built.metadataCid);
    assert.equal(report.ok, false);
    assert.match(report.error ?? JSON.stringify(report), /tamper|multihash/i);
  });
});

// --------------------------------------------------------------- cycles

/**
 * A Verifier whose content-authentication seam is neutralized, so we can
 * relabel real blocks and synthesize a cycle. The traversal stack guard is
 * independent of authentication — that is the code path under test.
 */
class CycleProbeVerifier extends Verifier {
  protected override requireBlock(cid: CID): { data: Buffer; row: BlockRow } {
    const row = (this as unknown as { blocks: BlockGetter }).blocks.getBlock(cid)!;
    return { data: Buffer.from(row.data), row };
  }
  protected override hashCheck(): Check {
    return { name: 'sha256-content-address', ok: true, basis: 'probe: authentication bypassed' };
  }
}

describe('cyclic references', () => {
  it('aborts traversal when a link closes a cycle back to an ancestor', async () => {
    const leaf = await h.ingest(PNG_1PX, 0x55);
    // Real, acyclic nodes: A links L; B links A.
    const nodeA = encodeDagPb({
      links: [{ hash: parseCID(leaf.cid), name: '', tsize: BigInt(PNG_1PX.length) }],
      data: fileUnixfs(PNG_1PX.length, [PNG_1PX.length]),
    });
    const a = await h.ingest(nodeA);
    const nodeB = encodeDagPb({
      links: [{ hash: parseCID(a.cid), name: '', tsize: BigInt(nodeA.length) }],
      data: fileUnixfs(PNG_1PX.length * 4, [nodeA.length]),
    });
    const b = await h.ingest(nodeB);

    // Forge: requests for A's label are served B's bytes; B links A, so
    // starting at B: B -> A(label) served B-bytes -> A(label) again => cycle.
    const forged: BlockGetter = {
      getBlock(cid: CID): BlockRow | undefined {
        if (cidToString(cid) === a.cid) {
          return h.store.getBlock(parseCID(b.cid));
        }
        return h.store.getBlock(cid);
      },
    };
    const v = new CycleProbeVerifier(forged, h.services.config);
    const report = v.resolveIpfsRef(
      { field: 'image', uri: `ipfs://${b.cid}`, cidText: b.cid, path: [] },
      h.services.config.maxMediaBytes,
    );
    assert.equal(report.ok, false);
    assert.match(report.error ?? '', /cycle|cyclic reference/);
  });

  it('accepts a legitimate diamond/DAG that repeats a CID (no false positive)', async () => {
    const leaf = await h.ingest(PNG_1PX, 0x55);
    const node = encodeDagPb({
      links: [
        { hash: parseCID(leaf.cid), name: '', tsize: BigInt(PNG_1PX.length) },
        { hash: parseCID(leaf.cid), name: '', tsize: BigInt(PNG_1PX.length) },
      ],
      data: fileUnixfs(PNG_1PX.length * 2, [PNG_1PX.length, PNG_1PX.length]),
    });
    const stored = await h.ingest(node);
    const metaJson = Buffer.from(JSON.stringify({ name: 'Dup', image: `ipfs://${stored.cid}` }), 'utf-8');
    const metaNode = encodeDagPb({ links: [], data: encodeUnixFsFile(metaJson) });
    const meta = await h.ingest(metaNode);
    const { body } = await verifyMeta(h.app, meta.cid);
    assert.equal(body.ok, true, body.report.error);
    assert.equal(body.report.media[0]!.payloadBytes, PNG_1PX.length * 2);
  });
});

// --------------------------------------------------------- encoding rules

describe('encoding rejection — no shape-only checks', () => {
  it('rejects non-UTF-8 metadata bytes', async () => {
    const node = encodeDagPb({ links: [], data: encodeUnixFsFile(Uint8Array.from([0x7b, 0xff, 0xfe, 0x7d])) });
    const stored = await h.ingest(node);
    const { status, body } = await verifyMeta(h.app, stored.cid);
    assert.equal(status, 422);
    assert.equal(body.ok, false);
    assert.match(JSON.stringify(body.report), /not valid for encoding utf-8|json/i);
  });

  it('rejects duplicate JSON keys', async () => {
    const built = await buildSimpleNft(h);
    const json = Buffer.from(`{"name":"a","name":"b","image":"ipfs://${built.imageCid}"}`);
    const node = encodeDagPb({ links: [], data: encodeUnixFsFile(json) });
    const stored = await h.ingest(node);
    const { body } = await verifyMeta(h.app, stored.cid);
    assert.equal(body.ok, false);
    assert.match(JSON.stringify(body.report), /duplicate JSON object key/);
  });

  it('rejects unknown metadata fields (whitelist model)', async () => {
    const built = await buildSimpleNft(h);
    const json = Buffer.from(
      `{"name":"x","image":"ipfs://${built.imageCid}","mint_transaction":"secret"}`,
    );
    const node = encodeDagPb({ links: [], data: encodeUnixFsFile(json) });
    const stored = await h.ingest(node);
    const { body } = await verifyMeta(h.app, stored.cid);
    assert.equal(body.ok, false);
    assert.match(JSON.stringify(body.report), /unknown metadata field.*mint_transaction/);
  });

  it('rejects payload with no recognized media magic signature', async () => {
    const garbage = Buffer.from(Array.from({ length: 128 }, (_, i) => (i * 31 + 7) & 0xff));
    const node = encodeDagPb({ links: [], data: encodeUnixFsFile(garbage) });
    const stored = await h.ingest(node);
    const metaJson = Buffer.from(JSON.stringify({ name: 'G', image: `ipfs://${stored.cid}` }), 'utf-8');
    const metaNode = encodeDagPb({ links: [], data: encodeUnixFsFile(metaJson) });
    const meta = await h.ingest(metaNode);
    const { body } = await verifyMeta(h.app, meta.cid);
    assert.equal(body.ok, false);
    assert.equal(body.report.media[0]!.ok, false);
    assert.match(
      body.report.media[0]!.checks.find((c) => c.name === 'media-encoding')!.basis,
      /unknown encoding/,
    );
  });

  it('rejects an http gateway reference instead of an ipfs:// CID', async () => {
    const built = await buildSimpleNft(h, { image: 'https://ipfs.io/ipfs/QmSomeGateway' });
    const { body } = await verifyMeta(h.app, built.metadataCid);
    assert.equal(body.ok, false);
    assert.match(JSON.stringify(body.report), /only content-addressed ipfs:\/\//);
  });

  it('rejects a block over the size limit (413)', async () => {
    const big = Buffer.alloc(h.services.config.maxBlockSize + 1, 0x61);
    const cid = cidToString(cidFromData(0x55, big));
    const res = await h.app.inject({
      method: 'POST',
      url: '/api/v1/blocks',
      payload: { cid, dataBase64: big.toString('base64') },
    });
    assert.equal(res.statusCode, 413);
  });

  it('rejects media whose assembled size exceeds the media limit', async () => {
    const small = await makeHarness({ maxMediaBytes: 32 });
    const built = await buildSimpleNft(small);
    const { status, body } = await verifyMeta(small.app, built.metadataCid);
    assert.equal(status, 422);
    assert.equal(body.ok, false);
    assert.match(JSON.stringify(body.report), /exceeds limit 32|size/);
    await small.close();
  });
});

// ----------------------------------------------- UnixFS structural checks

describe('UnixFS structural consistency', () => {
  it('rejects a wrong declared Tsize against the computed cumulative size', async () => {
    const leaf = await h.ingest(PNG_1PX, 0x55);
    const node = encodeDagPb({
      links: [{ hash: parseCID(leaf.cid), name: '', tsize: BigInt(PNG_1PX.length) + 999n }],
      data: fileUnixfs(PNG_1PX.length, [PNG_1PX.length]),
    });
    const stored = await h.ingest(node);
    const metaJson = Buffer.from(JSON.stringify({ name: 'T', image: `ipfs://${stored.cid}` }), 'utf-8');
    const metaNode = encodeDagPb({ links: [], data: encodeUnixFsFile(metaJson) });
    const meta = await h.ingest(metaNode);
    const { body } = await verifyMeta(h.app, meta.cid);
    assert.equal(body.ok, false);
    assert.match(JSON.stringify(body.report), /Tsize|cumulative/);
  });

  it('rejects a wrong blocksizes entry against the resolved chunk size', async () => {
    const leaf = await h.ingest(PNG_1PX, 0x55);
    const node = encodeDagPb({
      links: [{ hash: parseCID(leaf.cid), name: '', tsize: BigInt(PNG_1PX.length) }],
      data: fileUnixfs(PNG_1PX.length, [7]), // claims 7-byte chunk
    });
    const stored = await h.ingest(node);
    const metaJson = Buffer.from(JSON.stringify({ name: 'B', image: `ipfs://${stored.cid}` }), 'utf-8');
    const metaNode = encodeDagPb({ links: [], data: encodeUnixFsFile(metaJson) });
    const meta = await h.ingest(metaNode);
    const { body } = await verifyMeta(h.app, meta.cid);
    assert.equal(body.ok, false);
    assert.match(JSON.stringify(body.report), /blocksizes|disagrees/);
  });

  it('rejects a non-file UnixFS type (directory) as a media payload target', async () => {
    const img = await h.ingest(PNG_1PX, 0x55);
    const dirNode = encodeDagPb({
      links: [{ hash: parseCID(img.cid), name: 'image.png', tsize: BigInt(PNG_1PX.length) }],
      data: Uint8Array.from([0x08, 0x01]),
    });
    const dir = await h.ingest(dirNode);
    const metaJson = Buffer.from(JSON.stringify({ name: 'D', image: `ipfs://${dir.cid}` }), 'utf-8'); // no path
    const metaNode = encodeDagPb({ links: [], data: encodeUnixFsFile(metaJson) });
    const meta = await h.ingest(metaNode);
    const { body } = await verifyMeta(h.app, meta.cid);
    assert.equal(body.ok, false);
    assert.match(JSON.stringify(body.report), /directory|file payload/);
  });
});

// --------------------------------------- revisions, confirmation and reorg

describe('metadata revisions, confirmation window and reorg', () => {
  const tokenId = 'chain-token-1';

  const putMeta = (cid: string, height: number, blockHash: string) =>
    h.app.inject({
      method: 'PUT',
      url: `/api/v1/tokens/${tokenId}/metadata`,
      payload: { cid, height, blockHash },
    });

  const tip = (height: number, blockHash: string) =>
    h.app.inject({
      method: 'POST',
      url: '/api/v1/chain/tip',
      payload: { height, blockHash },
    });

  it('proposes, keeps the update unconfirmed, then confirms after N blocks', async () => {
    const built = await buildSimpleNft(h, {}, PNG_1PX);
    let res = await putMeta(built.metadataCid, 1000, '0xaaaa0001');
    assert.equal(res.statusCode, 201, res.payload);
    let body = JSON.parse(res.payload);
    assert.equal(body.status, 'proposed');

    // advance to 1001 (confirmations=3): still pending
    await tip(1001, '0xbbbb0002');
    res = await h.app.inject({ method: 'GET', url: `/api/v1/tokens/${tokenId}` });
    body = JSON.parse(res.payload);
    assert.equal(body.current, null);
    assert.ok(body.pending);

    // advance to 1002 (2 blocks on top): still pending
    await tip(1002, '0xcccc0003');
    res = await h.app.inject({ method: 'GET', url: `/api/v1/tokens/${tokenId}` });
    body = JSON.parse(res.payload);
    assert.equal(body.current, null);
    assert.ok(body.pending);

    // advance to 1003: proposal at h1000 now has 3 confirmations (1001..1003)
    await tip(1003, '0xcccc0004');
    res = await h.app.inject({ method: 'GET', url: `/api/v1/tokens/${tokenId}` });
    body = JSON.parse(res.payload);
    assert.equal(body.current?.version, 1);
    assert.equal(body.current?.cid, built.metadataCid);
    assert.equal(body.history[0]!.status, 'confirmed');
  });

  it('preserves the old CID as historical when a revision is superseded', async () => {
    const v1Cid = (await h.app.inject({ method: 'GET', url: `/api/v1/tokens/${tokenId}` })).json().current.cid;
    const built2 = await buildSimpleNft(h, { name: 'Second Revision' }, PNG_1PX);
    let res = await putMeta(built2.metadataCid, 1100, '0xdddd0004');
    assert.equal(res.statusCode, 201);
    // height 1100 is ahead of the current tip (1003), so it starts pending
    assert.equal(JSON.parse(res.payload).status, 'proposed');
    await tip(1103, '0xeeee0005');
    res = await h.app.inject({ method: 'GET', url: `/api/v1/tokens/${tokenId}` });
    const body = JSON.parse(res.payload);
    assert.equal(body.current.version, 2);
    const old = body.history.find((x: { version: number }) => x.version === 1)!;
    assert.equal(old.status, 'historical');
    assert.equal(old.cid, v1Cid); // old CID retained, not overwritten
  });

  it('rejects a duplicate revision (same CID) as a conflict', async () => {
    const res = await h.app.inject({ method: 'GET', url: `/api/v1/tokens/${tokenId}` });
    const currentCid = JSON.parse(res.payload).current.cid;
    const dup = await putMeta(currentCid, 1200, '0xffff0006');
    assert.equal(dup.statusCode, 409);
    assert.match(dup.payload, /already recorded|append-only/);
  });

  it('reorg undoes an unconfirmed update and leaves the confirmed CID effective', async () => {
    const token = 'reorg-token-1';
    const built = await buildSimpleNft(h, {}, PNG_1PX);
    let res = await h.app.inject({
      method: 'PUT',
      url: `/api/v1/tokens/${token}/metadata`,
      payload: { cid: built.metadataCid, height: 1300, blockHash: '0x11110001' },
    });
    assert.equal(res.statusCode, 201);

    // reorg BEFORE confirmation depth
    res = await h.app.inject({
      method: 'POST',
      url: '/api/v1/chain/reorg',
      payload: { height: 1300, oldHash: '0x11110001', newHash: '0x22220002' },
    });
    assert.equal(res.statusCode, 200, res.payload);
    const body = JSON.parse(res.payload);
    assert.equal(body.reverted.length, 1);
    assert.equal(body.reverted[0]!.cid, built.metadataCid);

    // token now has no current and the revoked row is marked reorged
    res = await h.app.inject({ method: 'GET', url: `/api/v1/tokens/${token}` });
    const tokenState = JSON.parse(res.payload);
    assert.equal(tokenState.current, null);
    assert.equal(tokenState.history[0]!.status, 'reorged');

    // a new proposal for the same (content-valid) CID is allowed after reorg,
    // and then confirms normally
    res = await h.app.inject({
      method: 'PUT',
      url: `/api/v1/tokens/${token}/metadata`,
      payload: { cid: built.metadataCid, height: 1300, blockHash: '0x22220002' },
    });
    assert.equal(res.statusCode, 201, res.payload);
  });

  it('never silently reverts an already-confirmed version on reorg', async () => {
    const token = 'reorg-token-2';
    const built = await buildSimpleNft(h, {}, PNG_1PX);
    await h.app.inject({
      method: 'PUT',
      url: `/api/v1/tokens/${token}/metadata`,
      payload: { cid: built.metadataCid, height: 1400, blockHash: '0x33330001' },
    });
    await tip(1403, '0x44440002');
    const current = JSON.parse((await h.app.inject({ method: 'GET', url: `/api/v1/tokens/${token}` })).payload).current;
    assert.ok(current);

    const res = await h.app.inject({
      method: 'POST',
      url: '/api/v1/chain/reorg',
      payload: { height: 1400, oldHash: '0x33330001', newHash: '0x55550003' },
    });
    const body = JSON.parse(res.payload);
    assert.equal(body.reverted.length, 0);
    assert.equal(body.protectedConfirmed.length, 1);
    const after = JSON.parse((await h.app.inject({ method: 'GET', url: `/api/v1/tokens/${token}` })).payload);
    assert.equal(after.current.cid, built.metadataCid);
  });

  it('emits an append-only PROPOSE/CONFIRM/REORG event ledger', async () => {
    const res = await h.app.inject({ method: 'GET', url: '/api/v1/events?limit=1000' });
    const events = JSON.parse(res.payload).events as { type: string }[];
    const kinds = new Set(events.map((e) => e.type));
    assert.ok(kinds.has('PROPOSE'));
    assert.ok(kinds.has('CONFIRM'));
    assert.ok(kinds.has('REORG'));
  });
});
