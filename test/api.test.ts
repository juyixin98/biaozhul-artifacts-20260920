import { test } from "node:test";
import assert from "node:assert/strict";
import { FastifyInstance } from "fastify";
import {
  makeTestApp,
  closeHarness,
  PNG_1PX,
  PNG_RED,
  putBlock,
  appendBlock,
  makeMetadata,
} from "./helpers";
import { makeCidV1, CODEC_JSON, CODEC_RAW } from "../src/crypto/cid";

interface BuiltDag {
  rootCid: string;
  imageCid: string;
  image: Buffer;
  root: Buffer;
}

/** Store image + metadata for token metadata version n and return CIDs. */
async function buildVersion(
  app: FastifyInstance,
  image: Buffer,
  version: number,
  name = "Test Token"
): Promise<BuiltDag> {
  const imageCid = makeCidV1(CODEC_RAW, image).canonical;
  const root = makeMetadata({
    name,
    version,
    image: imageCid,
    image_type: "image/png",
    image_size: image.length,
  });
  const rootCid = makeCidV1(CODEC_JSON, root).canonical;
  const r1 = await putBlock(app, imageCid, image);
  const r2 = await putBlock(app, rootCid, root);
  assert.ok(r1.statusCode === 201 || r1.statusCode === 200);
  assert.ok(r2.statusCode === 201 || r2.statusCode === 200);
  return { rootCid, imageCid, image, root };
}

test("full happy path: ingest, verify, register, confirm, inspect history", async () => {
  const h = await makeTestApp();
  const app = h.app;
  await appendBlock(app, "0x00000000a1700001", null); // h0

  const v1 = await buildVersion(app, PNG_1PX, 1);
  const reg = await app.inject({
    method: "PUT",
    url: "/tokens/token-1",
    payload: { root_cid: v1.rootCid },
  });
  assert.equal(reg.statusCode, 201, reg.body);
  const regBody = reg.json();
  assert.equal(regBody.version, 1);
  assert.equal(regBody.status, "pending");
  assert.equal(regBody.confirmations, 1);

  // Two more blocks -> 3 confirmations -> active/finalized.
  await appendBlock(app, "0x00000000a1700002", "0x00000000a1700001");
  await appendBlock(app, "0x00000000a1700003", "0x00000000a1700002");

  const token = (await app.inject({ method: "GET", url: "/tokens/token-1" })).json();
  assert.equal(token.currentStatus, "active");
  assert.equal(token.confirmations, 3);
  assert.equal(token.currentCid, v1.rootCid);

  // Evidence snapshot is preserved.
  const evRes = await app.inject({
    method: "GET",
    url: "/tokens/token-1/revisions/1/evidence",
  });
  assert.equal(evRes.statusCode, 200);
  const ev = evRes.json().evidence;
  assert.equal(ev.tree.contentAddressValid, true);
  assert.equal(ev.tree.children[0].mediaTypeDetected, "image/png");

  await closeHarness(h);
});

test("duplicate / old metadata version is rejected (VERSION_CONFLICT)", async () => {
  const h = await makeTestApp();
  const app = h.app;
  await appendBlock(app, "0xaaaaaaaa00000001", null);
  const v1 = await buildVersion(app, PNG_1PX, 1);
  await app.inject({ method: "PUT", url: "/tokens/t", payload: { root_cid: v1.rootCid } });

  // Re-registering version 1 with different content but same version field.
  const dup = await buildVersion(app, PNG_RED, 1, "Test Token");
  const res = await app.inject({
    method: "PUT",
    url: "/tokens/t",
    payload: { root_cid: dup.rootCid },
  });
  assert.equal(res.statusCode, 409);
  assert.equal(res.json().error, "VERSION_CONFLICT");

  // Skipping to version 3 is also rejected.
  const skip = await buildVersion(app, PNG_1PX, 3);
  const res2 = await app.inject({
    method: "PUT",
    url: "/tokens/t",
    payload: { root_cid: skip.rootCid },
  });
  assert.equal(res2.statusCode, 409);
  assert.equal(res2.json().details.expectedVersion, 2);

  await closeHarness(h);
});

test("reorg revokes the unconfirmed update and restores the old CID", async () => {
  const h = await makeTestApp();
  const app = h.app;
  // Genesis + 2 confirmations: v1 will be finalized at h2.
  await appendBlock(app, "0xbb00000000000001", null);
  const v1 = await buildVersion(app, PNG_1PX, 1);
  await app.inject({ method: "PUT", url: "/tokens/tk", payload: { root_cid: v1.rootCid } });
  await appendBlock(app, "0xbb00000000000002", "0xbb00000000000001");
  await appendBlock(app, "0xbb00000000000003", "0xbb00000000000002");
  let state = (await app.inject({ url: "/tokens/tk" })).json();
  assert.equal(state.currentStatus, "active");

  // v2 lands on the tip h3 — unconfirmed.
  await appendBlock(app, "0xbb00000000000004", "0xbb00000000000003");
  const v2 = await buildVersion(app, PNG_RED, 2);
  const reg2 = await app.inject({
    method: "PUT",
    url: "/tokens/tk",
    payload: { root_cid: v2.rootCid },
  });
  assert.equal(reg2.statusCode, 201);
  state = (await app.inject({ url: "/tokens/tk" })).json();
  assert.equal(state.currentCid, v2.rootCid);
  assert.equal(state.currentStatus, "pending");

  // Reorg replaces h3 with a different block. v2 anchored on the old h3 slot
  // becomes revoked; current CID must fall back to the finalized v1.
  const reorg = await app.inject({
    method: "POST",
    url: "/chain/blocks",
    payload: {
      action: "reorg",
      from_height: 3,
      new_blocks: [{ hash: "0xcc00000000000009", parent: "0xbb00000000000003" }],
    },
  });
  assert.equal(reorg.statusCode, 200, reorg.body);

  state = (await app.inject({ url: "/tokens/tk" })).json();
  assert.equal(state.currentCid, v1.rootCid);
  assert.equal(state.currentStatus, "active");

  const history = (await app.inject({ url: "/tokens/tk/history" })).json();
  const statuses = Object.fromEntries(history.revisions.map((r: any) => [r.version, r.status]));
  assert.deepEqual(statuses, { 1: "active", 2: "revoked" });
  // Old CID and anchor are retained even after revocation.
  const revoked = history.revisions.find((r: any) => r.version === 2);
  assert.equal(revoked.cid, v2.rootCid);
  assert.equal(revoked.anchorHeight, 3);

  await closeHarness(h);
});

test("deep reorg beyond finality is rejected", async () => {
  const h = await makeTestApp();
  const app = h.app;
  for (let i = 0; i < 5; i++) {
    await appendBlock(
      app,
      "0xdd" + String(i + 1).padStart(14, "0"),
      i === 0 ? null : "0xdd" + String(i).padStart(14, "0")
    );
  }
  // tip h4; replacing from h1 removes 4 blocks > finality 3.
  const res = await app.inject({
    method: "POST",
    url: "/chain/blocks",
    payload: {
      action: "reorg",
      from_height: 1,
      new_blocks: [{ hash: "0xee00000000000001", parent: "0xdd00000000000000" }],
    },
  });
  assert.equal(res.statusCode, 409);
  assert.equal(res.json().error, "DEEP_REORG_REJECTED");
  // Chain untouched.
  const chain = (await app.inject({ url: "/chain" })).json();
  assert.equal(chain.tipHeight, 4);
  await closeHarness(h);
});

test("tampered bytes are refused at PUT /blocks with digest evidence", async () => {
  const h = await makeTestApp();
  const app = h.app;
  const cid = makeCidV1(CODEC_RAW, PNG_1PX).canonical;
  const evil = Buffer.from(PNG_1PX);
  evil[12] ^= 0x01;
  const res = await app.inject({
    method: "PUT",
    url: `/blocks/${cid}`,
    payload: { data_base64: evil.toString("base64") },
  });
  assert.equal(res.statusCode, 422);
  assert.equal(res.json().error, "CONTENT_MISMATCH");
  assert.notEqual(res.json().details.claimedDigest, res.json().details.computedDigest);
  await closeHarness(h);
});

test("non-UTF8 metadata uploads but verification fails honestly", async () => {
  const h = await makeTestApp();
  const app = h.app;
  const bad = Buffer.from([0x7b, 0xff, 0xfe, 0x7d]);
  const cid = makeCidV1(CODEC_JSON, bad).canonical;
  const put = await app.inject({
    method: "PUT",
    url: `/blocks/${cid}`,
    payload: { data_base64: bad.toString("base64") },
  });
  assert.equal(put.statusCode, 201); // valid content address; bytes are what they are
  const verify = await app.inject({ method: "POST", url: "/verify", payload: { root_cid: cid } });
  assert.equal(verify.statusCode, 422);
  assert.equal(verify.json().error, "INVALID_JSON");
  await closeHarness(h);
});

test("unknown CID encodings are rejected at the API boundary", async () => {
  const h = await makeTestApp();
  const app = h.app;
  for (const bad of ["f0155" , "not-a-cid", "zb2rhbadshape"]) {
    const res = await app.inject({
      method: "GET",
      url: `/blocks/${encodeURIComponent(bad)}`,
    });
    assert.equal(res.statusCode, 400, `${bad} should be rejected`);
    assert.equal(res.json().error, "UNSUPPORTED_CID");
  }
  await closeHarness(h);
});

test("registering before any chain block exists is rejected", async () => {
  const h = await makeTestApp();
  const app = h.app;
  const v1 = await buildVersion(app, PNG_1PX, 1);
  const res = await app.inject({
    method: "PUT",
    url: "/tokens/x",
    payload: { root_cid: v1.rootCid },
  });
  assert.equal(res.statusCode, 400);
  await closeHarness(h);
});

test("appending a block with wrong parent is a CHAIN_CONFLICT", async () => {
  const h = await makeTestApp();
  const app = h.app;
  await appendBlock(app, "0xef00000000000001", null);
  const res = await app.inject({
    method: "POST",
    url: "/chain/blocks",
    payload: {
      action: "append",
      block_hash: "0xef00000000000002",
      parent_hash: "0xdeadbeefdeadbeef",
    },
  });
  assert.equal(res.statusCode, 409);
  assert.equal(res.json().error, "CHAIN_CONFLICT");
  await closeHarness(h);
});

test("ad-hoc verify of missing block returns 404 with reference path", async () => {
  const h = await makeTestApp();
  const app = h.app;
  const imageCid = makeCidV1(CODEC_RAW, PNG_1PX).canonical; // not uploaded
  const root = makeMetadata({
    name: "x",
    version: 1,
    image: imageCid,
    image_type: "image/png",
    image_size: PNG_1PX.length,
  });
  const rootCid = makeCidV1(CODEC_JSON, root).canonical;
  await putBlock(app, rootCid, root);
  const res = await app.inject({ method: "POST", url: "/verify", payload: { root_cid: rootCid } });
  assert.equal(res.statusCode, 404);
  assert.equal(res.json().error, "BLOCK_NOT_FOUND");
  assert.equal(res.json().details.refPath, "$.image");
  await closeHarness(h);
});

test("base64url and malformed padding are rejected rather than tolerated", async () => {
  const h = await makeTestApp();
  const app = h.app;
  const cid = makeCidV1(CODEC_RAW, PNG_1PX).canonical;
  const res1 = await app.inject({
    method: "PUT",
    url: `/blocks/${cid}`,
    payload: { data_base64: "AAAA----" },
  });
  assert.equal(res1.json().error, "INVALID_BASE64");
  const res2 = await app.inject({
    method: "PUT",
    url: `/blocks/${cid}`,
    payload: { data_base64: "AAA" },
  });
  assert.equal(res2.json().error, "INVALID_BASE64");
  await closeHarness(h);
});
