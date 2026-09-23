/**
 * Test harness: an in-memory app plus helpers to build real, locally
 * content-addressed NFT DAGs and to synthesize malformed variants.
 */
import type { FastifyInstance } from 'fastify';
import { buildApp } from '../src/app.js';
import { cidFromData, cidToString, parseCID } from '../src/codec/cid.js';
import { encodeDagPb } from '../src/codec/dag-pb.js';
import { encodeUnixFsFile } from '../src/codec/unixfs.js';

export interface Harness {
  app: FastifyInstance;
  store: ReturnType<typeof buildApp>['services']['store'];
  verifier: ReturnType<typeof buildApp>['services']['verifier'];
  services: ReturnType<typeof buildApp>['services'];
  close(): Promise<void>;
  ingest(bytes: Uint8Array, codec?: 0x70 | 0x55): Promise<{ cid: string; status: number; body: any }>;
}

export async function makeHarness(overrides: Record<string, unknown> = {}): Promise<Harness> {
  const built = buildApp({ dbPath: ':memory:', logLevel: 'silent', ...overrides } as never);
  await built.app.ready();
  const { app, services, close } = built;

  const ingest: Harness['ingest'] = async (bytes, codec = 0x70) => {
    const cid = cidToString(cidFromData(codec, bytes));
    const res = await app.inject({
      method: 'POST',
      url: '/api/v1/blocks',
      payload: { cid, dataBase64: Buffer.from(bytes).toString('base64') },
    });
    return { cid, status: res.statusCode, body: await res.json() };
  };

  return { app, store: services.store, verifier: services.verifier, services, close, ingest };
}

/** A valid 1x1 PNG: real 8-byte signature + IHDR + IDAT + IEND. */
export const PNG_1PX = Buffer.from(
  '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4' +
    '890000000d49444154789c6360000100000500010d0a2db40000000049454e44ae426082',
  'hex',
);

/** Real ISO-BMFF ftyp box so the media sniffer recognizes video/mp4. */
export function fakeMp4(extra = 32): Buffer {
  const ftypBody = Buffer.concat([
    Buffer.from('isom'),
    Buffer.from([0, 0, 0, 0]),
    Buffer.from('isomavc1mp42'),
  ]).subarray(0, 24);
  const ftyp = box('ftyp', ftypBody);
  const mdat = box('mdat', Buffer.alloc(extra, 0x61));
  return Buffer.concat([ftyp, mdat]);
}

function box(type: string, body: Buffer): Buffer {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(8 + body.length, 0);
  return Buffer.concat([len, Buffer.from(type), body]);
}

export interface BuiltNft {
  metadataCid: string;
  imageCid: string;
  allBlocks: Map<string, Uint8Array>;
}

/** Inline single-block image, single metadata file. Returns the metadata CID. */
export async function buildSimpleNft(
  h: Harness,
  metadataOverrides: Record<string, unknown> = {},
  imagePayload: Uint8Array = PNG_1PX,
): Promise<BuiltNft> {
  const allBlocks = new Map<string, Uint8Array>();

  const imgNode = encodeDagPb({ links: [], data: encodeUnixFsFile(imagePayload) });
  const img = await h.ingest(imgNode);
  allBlocks.set(img.cid, imgNode);

  const base = {
    name: 'Test Token',
    description: 'harness token',
    image: `ipfs://${img.cid}`,
  };
  const metaJson = Buffer.from(JSON.stringify({ ...base, ...metadataOverrides }), 'utf-8');
  const metaNode = encodeDagPb({ links: [], data: encodeUnixFsFile(metaJson) });
  const meta = await h.ingest(metaNode);
  allBlocks.set(meta.cid, metaNode);
  return { metadataCid: meta.cid, imageCid: img.cid, allBlocks };
}

export async function postJson(app: FastifyInstance, url: string, payload: unknown, method?: 'POST' | 'PUT') {
  const httpMethod =
    method ?? (url.startsWith('/chain') || url.startsWith('/verify') || url === '/blocks' ? 'POST' : 'PUT');
  const res = await app.inject({
    method: httpMethod,
    url: url.startsWith('/') ? `/api/v1${url}` : url,
    payload: payload as Record<string, unknown>,
  });
  let body: unknown;
  try {
    body = JSON.parse(res.payload);
  } catch {
    body = { raw: res.payload };
  }
  return { status: res.statusCode, body };
}

export { encodeDagPb, encodeUnixFsFile, cidToString, parseCID };
