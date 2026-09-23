import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { parseStrictJson, jsonToNative } from '../src/codec/json-strict.js';

describe('strict JSON parser', () => {
  const parseBytes = (s: string | Uint8Array) =>
    parseStrictJson(typeof s === 'string' ? new TextEncoder().encode(s) : s);

  it('parses a normal document', () => {
    const v = parseBytes('{"a":[1,true,null,-2.5],"b":"x"}');
    assert.equal(v.kind, 'object');
    assert.deepEqual(jsonToNative(v), { a: [1, true, null, -2.5], b: 'x' });
  });

  it('rejects duplicate keys at any nesting level', () => {
    assert.throws(() => parseBytes('{"a":1,"a":2}'), /duplicate JSON object key "a"/);
    assert.throws(() => parseBytes('{"x":{"a":1,"a":2}}'), /duplicate/);
  });

  it('rejects non-UTF-8 byte sequences', () => {
    // 0xFF 0xFE are never legal UTF-8 lead bytes
    assert.throws(() => parseBytes(Uint8Array.from([0x7b, 0x22, 0x78, 0x22, 0x3a, 0x22, 0xff, 0xfe, 0x22, 0x7d])), /not valid|malformed|invalid/i);
  });

  it('rejects CESU-8 / modified-UTF-8 lone surrogate encoding ED A0 80', () => {
    // U+D800 encoded in UTF-8-looking bytes is invalid per Unicode
    assert.throws(() => parseBytes(Uint8Array.from([0xed, 0xa0, 0x80])), /not valid|malformed|invalid/i);
  });

  it('rejects a lone low/high surrogate written as \\u', () => {
    assert.throws(() => parseBytes('"\\uDC00"'), /lone low surrogate/);
    assert.throws(() => parseBytes('"\\uD800x"'), /lone high surrogate/);
  });

  it('accepts a properly paired surrogate escape as one code point', () => {
    const v = parseBytes('"\\uD83D\\uDE00"');
    assert.equal(v.kind, 'string');
    if (v.kind === 'string') assert.equal(v.value, '😀');
  });

  it('rejects comments, trailing commas, trailing data', () => {
    assert.throws(() => parseBytes('{/* c */}'), /unexpected|expected/i);
    assert.throws(() => parseBytes('[1,]'), /unexpected token|expected/i);
    assert.throws(() => parseBytes('{}garbage'), /unexpected character/);
  });

  it('rejects unquoted/control bytes inside strings', () => {
    assert.throws(() => parseBytes('"a\nb"'), /unescaped control/);
  });

  it('rejects NaN/Infinity and leading zeros', () => {
    assert.throws(() => parseBytes('NaN'), /unexpected token/);
    assert.throws(() => parseBytes('Infinity'), /unexpected token/);
    assert.throws(() => parseBytes('01'), /unexpected character|expected/i);
  });

  it('keeps integers beyond 2^53 losslessly as bigint', () => {
    const v = parseBytes('9007199254740993');
    assert.equal(v.kind, 'number');
    if (v.kind === 'number') assert.equal(typeof v.value, 'bigint');
  });

  it('rejects malformed number forms', () => {
    for (const bad of ['-', '1.', '1e', '-e', '0.1.2', '12a']) {
      assert.throws(() => parseBytes(bad), Error, `should reject ${bad}`);
    }
  });
});
