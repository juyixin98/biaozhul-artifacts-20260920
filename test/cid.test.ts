import { test } from "node:test";
import assert from "node:assert/strict";
import { encodeBase32, decodeBase32 } from "../src/crypto/base32";
import { encodeBase58, decodeBase58 } from "../src/crypto/base58";
import { encodeVarint, readVarint } from "../src/crypto/varint";
import { makeCidV0, makeCidV1, parseCid, CODEC_JSON, CODEC_RAW } from "../src/crypto/cid";

// RFC 4648 base32 vectors.
test("base32 matches RFC4648 vectors", () => {
  const vectors: Array<[string, string]> = [
    ["", ""],
    ["f", "my"],
    ["fo", "mzxq"],
    ["foo", "mzxw6"],
    ["foob", "mzxw6yq"],
    ["fooba", "mzxw6ytb"],
    ["foobar", "mzxw6ytboi======".replace(/=/g, "")],
  ];
  for (const [input, expected] of vectors) {
    assert.equal(encodeBase32(Buffer.from(input)), expected, `encode ${input}`);
    assert.deepEqual(decodeBase32(expected), Buffer.from(input), `decode ${expected}`);
  }
});

test("base32 rejects uppercase, padding chars and junk", () => {
  assert.throws(() => decodeBase32("MZXW6")); // uppercase (different multibase)
  assert.throws(() => decodeBase32("mzxw6===")); // padding
  assert.throws(() => decodeBase32("m!zxw"));
  assert.throws(() => decodeBase32("mai")); // length 3 mod 8 is an impossible group
});

test("base32 rejects non-canonical trailing bits", () => {
  // 'my' decodes 'f'; 'mz' differs in low bits and encodes a short byte with
  // nonzero remainder bits.
  assert.throws(() => decodeBase32("mz"));
});

test("base58 known vector round-trips (empty hash CID bytes)", () => {
  // sha256 multihash of empty input: 1220 + e3b0...
  const mh = Buffer.from("1220e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "hex");
  const enc = encodeBase58(mh);
  assert.ok(enc.startsWith("Qm"));
  assert.deepEqual(decodeBase58(enc), mh);
});

test("base58 rejects invalid characters", () => {
  assert.throws(() => decodeBase58("0abcd")); // 0 not in alphabet
  assert.throws(() => decodeBase58("Oabcd")); // capital O
  assert.throws(() => decodeBase58("labcd")); // lowercase l
});

test("varint round-trip and minimal encoding", () => {
  for (const v of [0, 1, 127, 128, 255, 16384, 2 ** 31, Number.MAX_SAFE_INTEGER]) {
    const enc = encodeVarint(v);
    const read = readVarint(enc);
    assert.equal(read.value, v);
    assert.equal(read.length, enc.length);
  }
  // Non-minimal zero group must be rejected.
  assert.throws(() => readVarint(Buffer.from([0x80, 0x00])));
});

test("CIDv1 known vector: raw CID of empty bytes", () => {
  const cid = makeCidV1(CODEC_RAW, Buffer.alloc(0));
  // Verified independently with Python (base64.b32encode of <01 55 | mh>).
  assert.equal(cid.canonical, "bafkreihdwdcefgh4dqkjv67uzcmw7ojee6xedzdetojuzjevtenxquvyku");
});

test("CIDv0 of 'Hello World!' matches independently computed value", () => {
  const cid = makeCidV0(Buffer.from("Hello World!"));
  assert.equal(cid.canonical, "QmWvQxTqbG2Z9HPJgG57jjwR154cKhbtJenbyYTWkjgF3e");
});

test("parse CID accepts b and z CIDv1 multibase and canonicalizes", () => {
  const data = Buffer.from("abc");
  const cid = makeCidV1(CODEC_JSON, data);
  const viaB = parseCid(cid.canonical);
  assert.equal(viaB.version, 1);
  assert.equal(viaB.codec, CODEC_JSON);
  const zForm = "z" + encodeBase58(cid.bytes);
  const viaZ = parseCid(zForm);
  assert.equal(viaZ.canonical, cid.canonical);
  assert.equal(viaZ.inputEncoding, "base58btc");
});

test("parse CID rejects unsupported multibase prefixes", () => {
  // base16 (f), base36 (k), base64 (m), base64url (u) are all refused.
  for (const bad of ["f0155", "k5A", "mQQ", "uQQ", "Bafk", "Qx123"]) {
    assert.throws(() => parseCid(bad), /unsupported|invalid|must start/, `should reject ${bad}`);
  }
});

test("parse CID rejects unknown multicodec", () => {
  // CIDv1 with codec 0x71 (dag-cbor), valid structure, unsupported here.
  const { multihashSha256 } = require("../src/crypto/multihash");
  const { encodeVarint } = require("../src/crypto/varint");
  const mh = multihashSha256(Buffer.from("x"));
  const bytes = Buffer.concat([encodeVarint(1), encodeVarint(0x71), mh.bytes]);
  const s = "b" + encodeBase32(bytes);
  assert.throws(() => parseCid(s), /unsupported multicodec/);
});

test("parse CID rejects non-sha256 multihash and truncated digest", () => {
  // identity multihash (code 0x00)
  const { encodeVarint } = require("../src/crypto/varint");
  const id = Buffer.concat([encodeVarint(0), encodeVarint(1), Buffer.from("a")]);
  const bytes = Buffer.concat([encodeVarint(1), encodeVarint(CODEC_RAW), id]);
  assert.throws(() => parseCid("b" + encodeBase32(bytes)), /unsupported hash function/);

  // sha2-256 with truncated 16-byte digest
  const truncated = Buffer.concat([
    encodeVarint(0x12),
    encodeVarint(16),
    Buffer.alloc(16, 1),
  ]);
  const bytes2 = Buffer.concat([encodeVarint(1), encodeVarint(CODEC_RAW), truncated]);
  assert.throws(() => parseCid("b" + encodeBase32(bytes2)), /digest must be 32 bytes/);
});

test("parse CID rejects CID versions other than 0/1", () => {
  const { multihashSha256 } = require("../src/crypto/multihash");
  const { encodeVarint } = require("../src/crypto/varint");
  const mh = multihashSha256(Buffer.from("x"));
  const bytes = Buffer.concat([encodeVarint(2), encodeVarint(CODEC_RAW), mh.bytes]);
  assert.throws(() => parseCid("b" + encodeBase32(bytes)), /unsupported CID version/);
});

test("parse CID rejects dag-pb CIDv1 but accepts dag-pb CIDv0", () => {
  const { multihashSha256 } = require("../src/crypto/multihash");
  const { encodeVarint } = require("../src/crypto/varint");
  const mh = multihashSha256(Buffer.from("x"));
  const bytes = Buffer.concat([encodeVarint(1), encodeVarint(0x70), mh.bytes]);
  assert.throws(() => parseCid("b" + encodeBase32(bytes)), /dag-pb/);
  assert.doesNotThrow(() => parseCid(makeCidV0(Buffer.from("x")).canonical));
});
