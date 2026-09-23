/**
 * Generates examples/ with REAL content: every CID is computed over the
 * actual file bytes (sha2-256), so examples are runnable end-to-end without
 * trusting any hard-coded hash.
 *
 *   node --import tsx scripts/make-examples.ts   (or: npm run make:examples)
 */
import { writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { makeCidV1, CODEC_JSON, CODEC_RAW } from "../src/crypto/cid";

const OUT = join(process.cwd(), "examples");
mkdirSync(OUT, { recursive: true });

function writeJson(name: string, value: unknown): string {
  const path = join(OUT, name);
  writeFileSync(path, JSON.stringify(value, null, 2) + "\n");
  return path;
}

// 1x1 transparent PNG, 67 bytes.
const png1 = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64"
);
// 1x1 red PNG.
const png2 = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
  "base64"
);

const imageCid1 = makeCidV1(CODEC_RAW, png1).canonical;
const imageCid2 = makeCidV1(CODEC_RAW, png2).canonical;

// A linked json block (license card) referencing v1 media.
const link1 = Buffer.from(
  JSON.stringify({
    name: "license-card",
    image: imageCid1,
    image_type: "image/png",
    image_size: png1.length,
  })
);
const linkCid1 = makeCidV1(CODEC_JSON, link1).canonical;

const meta1 = Buffer.from(
  JSON.stringify({
    name: "Demo Token #42",
    version: 1,
    image: imageCid1,
    image_type: "image/png",
    image_size: png1.length,
    links: [{ cid: linkCid1, name: "license" }],
  })
);
const rootCid1 = makeCidV1(CODEC_JSON, meta1).canonical;

const meta2 = Buffer.from(
  JSON.stringify({
    name: "Demo Token #42 (revised art)",
    version: 2,
    image: imageCid2,
    image_type: "image/png",
    image_size: png2.length,
  })
);
const rootCid2 = makeCidV1(CODEC_JSON, meta2).canonical;

writeFileSync(join(OUT, "image-v1.png"), png1);
writeFileSync(join(OUT, "image-v2.png"), png2);

writeJson("put-image-v1.json", { cid: imageCid1, data_base64: png1.toString("base64") });
writeJson("put-image-v2.json", { cid: imageCid2, data_base64: png2.toString("base64") });
writeJson("put-link-v1.json", { cid: linkCid1, data_base64: link1.toString("base64") });
writeJson("put-metadata-v1.json", { cid: rootCid1, data_base64: meta1.toString("base64") });
writeJson("put-metadata-v2.json", { cid: rootCid2, data_base64: meta2.toString("base64") });

// Deliberately non-UTF-8 bytes; content address is valid, JSON decode fails.
const nonUtf8 = Buffer.from([0x7b, 0x22, 0x6e, 0x61, 0x6d, 0x65, 0x22, 0x3a, 0xff, 0xfe, 0x7d]);
const nonUtf8Cid = makeCidV1(CODEC_JSON, nonUtf8).canonical;
writeJson("put-non-utf8.json", { cid: nonUtf8Cid, data_base64: nonUtf8.toString("base64") });

// Tampered image: the v1 CID but one flipped byte -> must be rejected.
const tampered = Buffer.from(png1);
tampered[10] ^= 0xff;
writeJson("put-tampered.json", { cid: imageCid1, data_base64: tampered.toString("base64") });

const manifest = {
  tokenId: "demo-token-42",
  imageCidV1: imageCid1,
  imageCidV2: imageCid2,
  linkCidV1: linkCid1,
  rootCidV1: rootCid1,
  rootCidV2: rootCid2,
  nonUtf8Cid,
  imageBytesV1: png1.length,
  imageBytesV2: png2.length,
  notes:
    "All CIDs are sha2-256 content addresses computed over the actual file bytes. " +
    "put-tampered.json reuses imageCidV1 but flips one byte, so it must be refused.",
};
writeJson("manifest.json", manifest);

console.log("examples written to", OUT);
console.log(JSON.stringify(manifest, null, 2));
