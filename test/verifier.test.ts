import { test } from "node:test";
import assert from "node:assert/strict";
import { createDeps } from "../src/app";
import { DagVerifier } from "../src/verifier";
import { BlockStore } from "../src/store/blockstore";
import { makeCidV1, CODEC_JSON, CODEC_RAW } from "../src/crypto/cid";
import { multihashSha256 } from "../src/crypto/multihash";
import { encodeVarint } from "../src/crypto/varint";
import { PNG_1PX, PNG_RED } from "./helpers";

function setup(): { blocks: BlockStore; verifier: DagVerifier; db: any } {
  const deps = createDeps(":memory:");
  return { blocks: deps.blocks, verifier: deps.verifier, db: deps.db };
}

function storeValid(blocks: BlockStore): { imageCid: string; rootCid: string; linkCid: string } {
  const image = PNG_1PX;
  const imageCid = makeCidV1(CODEC_RAW, image).canonical;
  blocks.put(makeCidV1(CODEC_RAW, image), image);

  const link = Buffer.from(
    JSON.stringify({
      name: "license",
      image: imageCid,
      image_type: "image/png",
      image_size: image.length,
      links: [],
    })
  );
  const linkCid = makeCidV1(CODEC_JSON, link).canonical;
  blocks.put(makeCidV1(CODEC_JSON, link), link);

  const root = Buffer.from(
    JSON.stringify({
      name: "CryptoPunk #1",
      version: 1,
      image: imageCid,
      image_type: "image/png",
      image_size: image.length,
      links: [{ cid: linkCid, name: "license" }],
    })
  );
  const rootCid = makeCidV1(CODEC_JSON, root).canonical;
  blocks.put(makeCidV1(CODEC_JSON, root), root);
  return { imageCid, rootCid, linkCid };
}

test("valid 3-layer DAG verifies with complete per-layer evidence", () => {
  const { blocks, verifier, db } = setup();
  const { rootCid } = storeValid(blocks);
  const result = verifier.verifyRoot(rootCid, 1);
  assert.equal(result.version, 1);
  assert.equal(result.nodesVisited, 3);

  const root = result.tree;
  assert.equal(root.multicodecName, "json");
  assert.equal(root.contentAddressValid, true);
  assert.equal(root.checks.every((c) => c.ok), true);

  const imgNode = root.children.find((c) => c.refPath === "$.image")!;
  assert.equal(imgNode.multicodecName, "raw");
  assert.equal(imgNode.mediaTypeDeclared, "image/png");
  assert.equal(imgNode.mediaTypeDetected, "image/png");
  assert.equal(imgNode.mediaTypeMatches, true);
  assert.equal(imgNode.sizeMatches, true);
  assert.ok(imgNode.checks.some((c) => c.id === "multihash-recompute"));

  const linkNode = root.children.find((c) => c.refPath === "$.links[0]")!;
  assert.equal(linkNode.name, "license");
  assert.equal(linkNode.multicodecName, "json");
  db.close();
});

test("tampered bytes are rejected by BlockService before they can be stored", () => {
  const { BlockService } = require("../src/services/block-service");
  const { blocks, db } = setup();
  const service = new BlockService(blocks);
  const original = PNG_1PX;
  const cid = makeCidV1(CODEC_RAW, original);
  const tampered = Buffer.from(original);
  tampered[10] ^= 0xff;
  try {
    service.ingest(cid.canonical, tampered);
    assert.fail("expected CONTENT_MISMATCH");
  } catch (e: any) {
    assert.equal(e.code, "CONTENT_MISMATCH");
    assert.ok(e.details.computedDigest !== e.details.claimedDigest);
  }
  assert.equal(blocks.getRecord(cid.canonical), null);
  db.close();
});

test("tampered stored bytes fail multihash recompute with claimed vs computed digests", () => {
  const { blocks, verifier, db } = setup();
  const { rootCid, imageCid } = storeValid(blocks);
  // Corrupt the image row directly (simulating storage-level tampering).
  const rec = blocks.getRecord(imageCid)!;
  const evil = Buffer.from(rec.data);
  evil[20] ^= 0x01;
  blocks.rawDb.prepare("UPDATE blocks SET data = ? WHERE cid = ?").run(evil, imageCid);
  try {
    verifier.verifyRoot(rootCid, 1);
    assert.fail("expected CONTENT_MISMATCH");
  } catch (e: any) {
    assert.equal(e.code, "CONTENT_MISMATCH");
    assert.ok(e.details.claimedDigest !== e.details.computedDigest);
  }
  db.close();
});

test("circular reference is rejected", () => {
  const { blocks, verifier, db } = setup();
  // Build mutually-referencing json blocks. With real content addressing this
  // is impossible to construct (each CID depends on the other), so the test
  // seam fabricates the store state the cycle guard must detect.
  const cidA = makeCidV1(CODEC_JSON, Buffer.from("A")).canonical;
  const cidB = makeCidV1(CODEC_JSON, Buffer.from("B")).canonical;
  const pngCid = makeCidV1(CODEC_RAW, PNG_1PX).canonical;

  const bodyA = Buffer.from(
    JSON.stringify({
      name: "A",
      version: 1,
      image: pngCid,
      image_type: "image/png",
      image_size: PNG_1PX.length,
      links: [{ cid: cidB }],
    })
  );
  const bodyB = Buffer.from(
    JSON.stringify({
      name: "B",
      image: pngCid,
      image_type: "image/png",
      image_size: PNG_1PX.length,
      links: [{ cid: cidA }],
    })
  );
  blocks.putUntrustedForTest(cidA, CODEC_JSON, bodyA);
  blocks.putUntrustedForTest(cidB, CODEC_JSON, bodyB);
  blocks.put(makeCidV1(CODEC_RAW, PNG_1PX), PNG_1PX);
  const cycleVerifier = new DagVerifier(blocks, true);
  try {
    cycleVerifier.verifyRoot(cidA, 1);
    assert.fail("expected DAG_CYCLE");
  } catch (e: any) {
    assert.equal(e.code, "DAG_CYCLE");
    assert.deepEqual(e.details.cycle, [cidA, cidB, cidA]);
  }
  db.close();
});

test("missing block is a typed BLOCK_NOT_FOUND with reference path", () => {
  const { blocks, verifier, db } = setup();
  const imageCid = makeCidV1(CODEC_RAW, PNG_1PX).canonical; // not stored
  const root = Buffer.from(
    JSON.stringify({
      name: "x",
      version: 1,
      image: imageCid,
      image_type: "image/png",
      image_size: PNG_1PX.length,
    })
  );
  const rootCid = makeCidV1(CODEC_JSON, root).canonical;
  blocks.put(makeCidV1(CODEC_JSON, root), root);
  try {
    verifier.verifyRoot(rootCid, 1);
    assert.fail("expected BLOCK_NOT_FOUND");
  } catch (e: any) {
    assert.equal(e.code, "BLOCK_NOT_FOUND");
    assert.equal(e.details.refPath, "$.image");
  }
  db.close();
});

test("non-UTF8 JSON bytes are rejected at the encoding layer", () => {
  const { blocks, verifier, db } = setup();
  const imageCid = makeCidV1(CODEC_RAW, PNG_1PX).canonical;
  blocks.put(makeCidV1(CODEC_RAW, PNG_1PX), PNG_1PX);
  // bytes hash to some valid CID; content is not UTF-8
  const badJson = Buffer.from([0x7b, 0xff, 0xfe, 0x7d]);
  const cid = makeCidV1(CODEC_JSON, badJson).canonical;
  blocks.put(makeCidV1(CODEC_JSON, badJson), badJson);
  try {
    verifier.verifyRoot(cid, 1);
    assert.fail("expected INVALID_JSON");
  } catch (e: any) {
    assert.equal(e.code, "INVALID_JSON");
    assert.match(e.message, /not valid UTF-8/);
  }
  db.close();
});

test("duplicate JSON keys are rejected even though content address is valid", () => {
  const { blocks, verifier, db } = setup();
  const bad = Buffer.from(
    `{"name":"x","version":1,"image":"${makeCidV1(CODEC_RAW, PNG_1PX).canonical}","image_type":"image/png","image_size":${PNG_1PX.length},"name":"y"}`
  );
  const cid = makeCidV1(CODEC_JSON, bad).canonical;
  blocks.put(makeCidV1(CODEC_JSON, bad), bad);
  try {
    verifier.verifyRoot(cid, 1);
    assert.fail("expected INVALID_JSON");
  } catch (e: any) {
    assert.equal(e.code, "INVALID_JSON");
    assert.match(e.message, /duplicate key/);
  }
  db.close();
});

test("declared media size mismatch is rejected", () => {
  const { blocks, verifier, db } = setup();
  const image = PNG_1PX;
  const imageCid = makeCidV1(CODEC_RAW, image).canonical;
  blocks.put(makeCidV1(CODEC_RAW, image), image);
  const root = Buffer.from(
    JSON.stringify({
      name: "x",
      version: 1,
      image: imageCid,
      image_type: "image/png",
      image_size: image.length + 10,
    })
  );
  const rootCid = makeCidV1(CODEC_JSON, root).canonical;
  blocks.put(makeCidV1(CODEC_JSON, root), root);
  try {
    verifier.verifyRoot(rootCid, 1);
    assert.fail("expected size mismatch");
  } catch (e: any) {
    assert.equal(e.code, "INVALID_METADATA");
    assert.match(e.message, /media size mismatch/);
  }
  db.close();
});

test("declared media type mismatch (PNG bytes, jpeg declared) is rejected", () => {
  const { blocks, verifier, db } = setup();
  const imageCid = makeCidV1(CODEC_RAW, PNG_1PX).canonical;
  blocks.put(makeCidV1(CODEC_RAW, PNG_1PX), PNG_1PX);
  const root = Buffer.from(
    JSON.stringify({
      name: "x",
      version: 1,
      image: imageCid,
      image_type: "image/jpeg",
      image_size: PNG_1PX.length,
    })
  );
  const rootCid = makeCidV1(CODEC_JSON, root).canonical;
  blocks.put(makeCidV1(CODEC_JSON, root), root);
  assert.throws(
    () => verifier.verifyRoot(rootCid, 1),
    (e: any) => e.code === "INVALID_METADATA" && /media type mismatch/.test(e.message)
  );
  db.close();
});

test("different image bytes produce a different CID", () => {
  assert.notEqual(
    makeCidV1(CODEC_RAW, PNG_1PX).canonical,
    makeCidV1(CODEC_RAW, PNG_RED).canonical
  );
});

test("unknown multibase / hash encodings cannot be used as references", () => {
  const { blocks, verifier, db } = setup();
  // base16 multibase 'f'
  const mh = multihashSha256(Buffer.from("x"));
  const bytes = Buffer.concat([encodeVarint(1), encodeVarint(CODEC_RAW), mh.bytes]);
  const fakeCid = "f" + bytes.toString("hex");
  const root = Buffer.from(
    JSON.stringify({
      name: "x",
      version: 1,
      image: fakeCid,
      image_type: "image/png",
      image_size: 1,
    })
  );
  const rootCid = makeCidV1(CODEC_JSON, root).canonical;
  blocks.put(makeCidV1(CODEC_JSON, root), root);
  try {
    verifier.verifyRoot(rootCid, 1);
    assert.fail("expected rejection of unsupported CID");
  } catch (e: any) {
    assert.ok(
      e.code === "UNSUPPORTED_CID" || e.code === "INVALID_METADATA",
      `got ${e.code}`
    );
    assert.match(e.message, /CID|multibase/);
  }
  db.close();
});

test("diamond DAG: same CID referenced twice is verified once and allowed", () => {
  const { blocks, verifier, db } = setup();
  const imageCid = makeCidV1(CODEC_RAW, PNG_1PX).canonical;
  blocks.put(makeCidV1(CODEC_RAW, PNG_1PX), PNG_1PX);

  // Shared link node referenced from two parents.
  const shared = Buffer.from(
    JSON.stringify({ name: "shared", image: imageCid, image_type: "image/png", image_size: PNG_1PX.length })
  );
  const sharedCid = makeCidV1(CODEC_JSON, shared).canonical;
  blocks.put(makeCidV1(CODEC_JSON, shared), shared);

  const root = Buffer.from(
    JSON.stringify({
      name: "diamond",
      version: 1,
      image: imageCid,
      image_type: "image/png",
      image_size: PNG_1PX.length,
      links: [{ cid: sharedCid, name: "a" }, { cid: sharedCid, name: "b" }],
    })
  );
  const rootCid = makeCidV1(CODEC_JSON, root).canonical;
  blocks.put(makeCidV1(CODEC_JSON, root), root);

  const result = verifier.verifyRoot(rootCid, 1);
  assert.equal(result.nodesVisited, 3); // root, shared once, image once
  const links = result.tree.children.filter((c) => c.refPath.startsWith("$.links"));
  assert.equal(links.length, 2);
  assert.ok(links.some((l) => l.alreadyVerified));
  db.close();
});

test("root metadata version is checked against expected revision", () => {
  const { blocks, verifier, db } = setup();
  const { rootCid } = storeValid(blocks);
  assert.throws(
    () => verifier.verifyRoot(rootCid, 2),
    (e: any) => e.code === "VERSION_CONFLICT"
  );
  db.close();
});
