import { test } from "node:test";
import assert from "node:assert/strict";
import { parseStrictJson } from "../src/codec/json-parse";
import { decodeUtf8Strict, InvalidUtf8Error } from "../src/codec/utf8";
import { sniffMediaType } from "../src/codec/media-sniff";
import { PNG_1PX, jpegBytes } from "./helpers";

test("strict JSON parses normal values", () => {
  assert.deepEqual(parseStrictJson('{"a":1,"b":[true,null,"x"]}'), {
    a: 1,
    b: [true, null, "x"],
  });
});

test("strict JSON rejects duplicate object keys", () => {
  assert.throws(() => parseStrictJson('{"a":1,"a":2}'), /duplicate key 'a'/);
  assert.throws(
    () => parseStrictJson('{"outer":{"x":1,"x":2}}'),
    /duplicate key 'x'/
  );
});

test("strict JSON rejects lone surrogate escapes", () => {
  assert.throws(() => parseStrictJson('"\\uD800"'), /lone high surrogate/);
  assert.throws(() => parseStrictJson('"\\uDC00"'), /lone low surrogate/);
  // Valid surrogate pair is accepted.
  assert.equal(parseStrictJson('"\\uD83D\\uDE00"'), "\u{1F600}");
});

test("strict JSON rejects leading zeros, plus signs and trailing data", () => {
  assert.throws(() => parseStrictJson("01"), /leading zeros/);
  assert.throws(() => parseStrictJson("+1"), /invalid number|leading '\+'/);
  assert.throws(() => parseStrictJson("1 2"), /trailing characters/);
  assert.throws(() => parseStrictJson('{"a":1,}'), /trailing comma/);
  assert.throws(() => parseStrictJson("[1,2,]"), /trailing comma/);
});

test("strict JSON rejects unescaped control chars and NaN/Infinity literals", () => {
  assert.throws(() => parseStrictJson('"a\nb"'), /unescaped control/);
  assert.throws(() => parseStrictJson("NaN"), /invalid literal|invalid number/);
  assert.throws(() => parseStrictJson("Infinity"), /invalid number|invalid literal/);
});

test("strict UTF-8 decodes valid multibyte text", () => {
  const text = "héllo 世界 \u{1F600}";
  assert.equal(decodeUtf8Strict(Buffer.from(text, "utf8")), text);
});

test("strict UTF-8 rejects every class of invalid sequence", () => {
  // stray continuation byte
  assert.throws(() => decodeUtf8Strict(Buffer.from([0x80])), InvalidUtf8Error);
  // truncated 2-byte
  assert.throws(() => decodeUtf8Strict(Buffer.from([0xc3])), /truncated/);
  // overlong encoding of '/' (2-byte C0 AF)
  assert.throws(() => decodeUtf8Strict(Buffer.from([0xc0, 0xaf])), /overlong/);
  // overlong 3-byte (E0 80 80 == NUL)
  assert.throws(() => decodeUtf8Strict(Buffer.from([0xe0, 0x80, 0x80])), /overlong/);
  // surrogate U+D800 (ED A0 80)
  assert.throws(() => decodeUtf8Strict(Buffer.from([0xed, 0xa0, 0x80])), /surrogate/);
  // above U+10FFFF (F4 90 80 80)
  assert.throws(() => decodeUtf8Strict(Buffer.from([0xf4, 0x90, 0x80, 0x80])), /above/);
});

test("Buffer.toString accepts invalid UTF-8 but strict decoder refuses", () => {
  const bad = Buffer.from([0xff, 0xfe, 0x61]);
  assert.equal(bad.toString("utf8").includes("a"), true); // would silently pass
  assert.throws(() => decodeUtf8Strict(bad), InvalidUtf8Error);
});

test("media sniff identifies PNG/JPEG/GIF/WebP from bytes", () => {
  const png = sniffMediaType(PNG_1PX);
  assert.ok(png && png.type === "image/png");
  const jpeg = sniffMediaType(jpegBytes());
  assert.ok(jpeg && jpeg.type === "image/jpeg");
  assert.equal(sniffMediaType(Buffer.from("GIF89a"))?.type, "image/gif");
  assert.equal(sniffMediaType(Buffer.from("GIF87a"))?.type, "image/gif");
  const webp = sniffMediaType(
    Buffer.concat([Buffer.from("RIFF"), Buffer.alloc(4), Buffer.from("WEBPVP8 ")])
  );
  assert.equal(webp?.type, "image/webp");
});

test("media sniff refuses unknown content", () => {
  assert.equal(sniffMediaType(Buffer.from("not an image at all")), null);
  assert.equal(sniffMediaType(Buffer.from([0x89, 0x50])), null); // truncated PNG
  assert.equal(sniffMediaType(Buffer.from("<svg></svg>")), null); // text-based format
});
