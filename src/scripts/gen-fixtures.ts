/**
 * Fixture generator — builds a *real* local content-addressed NFT DAG:
 *
 *   metadata (UnixFS file, dag-pb, inline JSON)
 *     └─ image       ipfs://bafy…/image.png   (UnixFS dir → small PNG leaf)
 *     └─ animation   ipfs://bafy…/clip.mp4    (sharded file DAG, raw leaves)
 *
 * It writes every block into the service SQLite database and exports:
 *   examples/payloads/metadata.json          human-readable metadata
 *   examples/blocks/<cid>.b64                one base64 block file per CID
 *   examples/blocks/all-blocks.json          {cid: base64} bundle
 *   examples/demo.sh                         end-to-end curl walkthrough
 *
 * Run: npm run gen-fixtures
 */
import { writeFileSync, mkdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { cidFromData, cidToString, parseCID } from '../codec/cid.js';
import { encodeDagPb, decodeDagPb, type PbLink } from '../codec/dag-pb.js';
import { encodeUnixFsFile, encodeUnixFsShardHeader } from '../codec/unixfs.js';
import { buildApp } from '../app.js';
import { loadConfig } from '../config.js';

const ROOT = join(process.cwd(), 'examples');
const BLOCK_DIR = join(ROOT, 'blocks');
const PAYLOAD_DIR = join(ROOT, 'payloads');
mkdirSync(BLOCK_DIR, { recursive: true });
mkdirSync(PAYLOAD_DIR, { recursive: true });

// Deterministic 1x1 transparent PNG (67 bytes) — real signature + IHDR.
const PNG_1PX = Buffer.from(
  '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4' +
    '890000000d49444154789c6360000100000500010d0a2db40000000049454e44ae426082',
  'hex',
);

function realMp4(): Buffer {
  // A structurally real ISO-BMFF skeleton: ftyp + free + mdat.
  const ftypBody = Buffer.concat([
    Buffer.from('isom'), // major brand
    Buffer.from([0, 0, 0, 0]), // minor version
    Buffer.from('isomavc1mp42'), // compatible brands (20 bytes padded to 20)
  ]).subarray(0, 24);
  const ftyp = box('ftyp', ftypBody);
  const payload = Buffer.alloc(320, 0x61); // fake but present media data unit
  const mdat = box('mdat', payload);
  return Buffer.concat([ftyp, mdat]);
}

function box(type: string, body: Buffer): Buffer {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(8 + body.length, 0);
  return Buffer.concat([len, Buffer.from(type), body]);
}

const config = loadConfig();
const { app, services, close } = buildApp({ dbPath: join(process.cwd(), 'data', 'fixtures.db'), logLevel: 'silent' });

interface Stored {
  cid: string;
  bytes: Uint8Array;
}

function storeBlock(bytes: Uint8Array, codec: 0x70 | 0x55 = 0x70): Stored {
  const cid = cidFromData(codec, bytes);
  const cidText = cidToString(cid);
  // verify before storing — even the fixture pipeline stores only valid blocks
  const report = services.verifier.verifyBlock(cid, bytes);
  if (!report.ok) throw new Error(`fixture block failed self-verification ${cidText}: ${report.error}`);
  services.store.putBlock(cid, codec === 0x70 ? 'dag-pb' : 'raw', bytes);
  return { cid: cidText, bytes };
}

function cumulativeDagSize(cid: string, bytesByCid: Map<string, Uint8Array>): bigint {
  // Mirror UnixFS Tsize semantics.
  const cached = tsizeCache.get(cid);
  if (cached !== undefined) return cached;
  const bytes = bytesByCid.get(cid)!;
  if (rawByCid.has(cid)) {
    const v = BigInt(bytes.length);
    tsizeCache.set(cid, v);
    return v;
  }
  const { node } = decodeDagPb(bytes);
  let total = BigInt(bytes.length);
  for (const l of node.links) total += cumulativeDagSize(cidToString(l.hash), bytesByCid);
  tsizeCache.set(cid, total);
  return total;
}
const tsizeCache = new Map<string, bigint>();
const rawByCid = new Set<string>();

async function main() {
  const all = new Map<string, Uint8Array>();
  const remember = (s: Stored, raw = false) => {
    all.set(s.cid, s.bytes);
    if (raw) rawByCid.add(s.cid);
    return s;
  };

  // ---- small PNG as a single inline UnixFS file ----
  const pngNode = encodeDagPb({ links: [], data: encodeUnixFsFile(PNG_1PX) });
  const pngBlock = remember(storeBlock(pngNode));

  // ---- image directory with a named link ----
  // UnixFS Data message: field1 varint Type=Directory(1) => 0x08 0x01
  const dirData = Uint8Array.from([0x08, 0x01]);
  const dirNode = encodeDagPb({
    links: [{ hash: parseCID(pngBlock.cid), name: 'image.png', tsize: cumulativeDagSize(pngBlock.cid, all) }],
    data: new Uint8Array(dirData),
  });
  const imageDir = remember(storeBlock(dirNode));

  // ---- sharded "mp4": two raw leaves + UnixFS file node ----
  const mp4 = realMp4();
  const mid = Math.ceil(mp4.length / 2);
  const partA = mp4.subarray(0, mid);
  const partB = mp4.subarray(mid);
  const leafA = remember(storeBlock(partA, 0x55), true);
  const leafB = remember(storeBlock(partB, 0x55), true);

  const fileNodeBytes = encodeShardedFile([partA.length, partB.length], [leafA, leafB], all);
  const clipBlock = remember(storeBlock(fileNodeBytes));

  // ---- metadata JSON ----
  const metadata = {
    name: 'Consistency Demo Token #123',
    description: 'Generated locally; every reference resolves through content-addressed blocks. No gateway.',
    image: `ipfs://${imageDir.cid}/image.png`,
    animation_url: `ipfs://${clipBlock.cid}`,
    external_url: 'https://example.invalid/optional-declared-only',
    attributes: [
      { trait_type: 'generation', value: 1 },
      { trait_type: 'palette', value: 'transparent' },
      { display_type: 'boost_number', trait_type: 'rarity', value: 99.5 },
    ],
    properties: { origin: 'local-fixture', hash: 'sha2-256' },
  };
  const jsonBytes = Buffer.from(JSON.stringify(metadata, null, 2) + '\n', 'utf-8');
  writeFileSync(join(PAYLOAD_DIR, 'metadata.json'), jsonBytes);
  const metaNode = encodeDagPb({ links: [], data: encodeUnixFsFile(jsonBytes) });
  const metaBlock = remember(storeBlock(metaNode));

  // ---- export block files ----
  const bundle: Record<string, string> = {};
  for (const [cid, bytes] of all) {
    const b64 = Buffer.from(bytes).toString('base64');
    bundle[cid] = b64;
    writeFileSync(join(BLOCK_DIR, `${cid}.b64`), b64);
  }
  writeFileSync(join(BLOCK_DIR, 'all-blocks.json'), JSON.stringify(bundle, null, 2));

  // ---- verify through the real engine ----
  const report = services.verifier.verifyMetadataCid(metaBlock.cid);
  if (!report.ok) {
    console.error(JSON.stringify(report, null, 2));
    throw new Error('fixture metadata failed verification');
  }

  const digestOf = (b: Uint8Array) => createHash('sha256').update(b).digest('hex').slice(0, 16);
  const summary = {
    metadataCid: metaBlock.cid,
    imageDirectoryCid: imageDir.cid,
    imageLeafCid: pngBlock.cid,
    animationCid: clipBlock.cid,
    rawLeafCids: [leafA.cid, leafB.cid],
    pngBytes: PNG_1PX.length,
    pngSha256Prefix: digestOf(PNG_1PX),
    mp4Bytes: mp4.length,
    blockCount: all.size,
    verificationOk: report.ok,
  };
  writeFileSync(join(ROOT, 'fixture-cids.json'), JSON.stringify(summary, null, 2));
  writeDemo(summary);

  console.log(JSON.stringify(summary, null, 2));
  await close();
}

function encodeShardedFile(
  sizes: number[],
  leaves: Stored[],
  all: Map<string, Uint8Array>,
): Uint8Array {
  const links: PbLink[] = leaves.map((leaf) => ({
    hash: parseCID(leaf.cid),
    name: '',
    tsize: cumulativeDagSize(leaf.cid, all),
  }));
  const total = sizes.reduce((a, b) => a + b, 0);
  const data = encodeUnixFsShardHeader();
  // append filesize + blocksizes to the UnixFS Data message
  const extra: number[] = [];
  pushVarintField(extra, 3, BigInt(total));
  for (const sz of sizes) pushVarintField(extra, 4, BigInt(sz));
  const full = Buffer.concat([Buffer.from(data), Buffer.from(extra)]);
  return encodeDagPb({ links, data: new Uint8Array(full) });
}

function pushVarintField(out: number[], field: number, value: bigint) {
  out.push((field << 3) | 0);
  let v = value;
  do {
    const b = Number(v & 0x7fn);
    v >>= 7n;
    out.push(v > 0n ? b | 0x80 : b);
  } while (v > 0n);
}

function writeDemo(s: { metadataCid: string; imageDirectoryCid: string; imageLeafCid: string; animationCid: string }) {
  const sh = `#!/usr/bin/env bash
# End-to-end acceptance walkthrough. Requires: npm run build-free fixtures first.
#   npm run gen-fixtures && npm start
set -euo pipefail
BASE=\${BASE:-http://127.0.0.1:3000/api/v1}
META=${s.metadataCid}
IMGDIR=${s.imageDirectoryCid}
ANIM=${s.animationCid}

echo "== ingest all blocks =="
node -e 'const b=require("./examples/blocks/all-blocks.json");(async()=>{for(const[cid,dataBase64]of Object.entries(b)){const r=await fetch(process.env.BASE+"/blocks",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({cid,dataBase64})});if(!r.ok&&r.status!==200){console.error(await r.text());process.exit(1)}}console.log("blocks ingested")})()'

echo "== full metadata verification (all layers) =="
curl -fsS -X POST "$BASE/verify/metadata" -H 'content-type: application/json' \\
  -d "{\\"cid\\":\\"$META\\"}" | node -e 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{const j=JSON.parse(d);console.log("ok=",j.ok," layers=",j.report.layers.length," media=",j.report.media.map(m=>m.media?.mime));if(!j.ok)process.exit(1)})'

echo "== propose revision at chain height 100 =="
curl -fsS -X PUT "$BASE/tokens/demo-token-123/metadata" -H 'content-type: application/json' \\
  -d "{\\"cid\\":\\"$META\\",\\"height\\":100,\\"blockHash\\":\\"0xaaaa1111\\"}" | head -c 400; echo

echo "== advance tip to 102 — proposal still unconfirmed (3 confirmations) =="
curl -fsS -X POST "$BASE/chain/tip" -H 'content-type: application/json' -d '{"height":102,"blockHash":"0xbbbb2222"}' >/dev/null

echo "== reorg at height 100 — unconfirmed update must be undone =="
curl -fsS -X POST "$BASE/chain/reorg" -H 'content-type: application/json' \\
  -d '{"height":100,"oldHash":"0xaaaa1111","newHash":"0xcccc3333"}'

echo
echo "== re-propose at 100, advance to 103 — revision becomes effective =="
curl -fsS -X PUT "$BASE/tokens/demo-token-123/metadata" -H 'content-type: application/json' \\
  -d "{\\"cid\\":\\"$META\\",\\"height\\":100,\\"blockHash\\":\\"0xcccc3333\\"}" >/dev/null
curl -fsS -X POST "$BASE/chain/tip" -H 'content-type: application/json' -d '{"height":103,"blockHash":"0xdddd4444"}' >/dev/null
curl -fsS "$BASE/tokens/demo-token-123" | node -e 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{const j=JSON.parse(d);console.log("current=",j.current?.version,j.current?.cid.slice(0,20));if(!j.current)process.exit(1)})'
`;
  writeFileSync(join(process.cwd(), 'examples', 'demo.sh'), sh, { mode: 0o755 });
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
