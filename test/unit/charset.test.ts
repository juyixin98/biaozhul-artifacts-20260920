import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  decodeRfc7578Bytes,
  decodeExtValue,
  assertNoEncodedWord,
} from '../../src/protocol/charset.js';
import { isMultipartError } from '../../src/protocol/errors.js';

test('decodeRfc7578Bytes passes ASCII through unchanged', () => {
  assert.equal(decodeRfc7578Bytes('report.txt', 'filename'), 'report.txt');
});

test('decodeRfc7578Bytes decodes valid UTF-8 (euro sign)', () => {
  // Simulate bytes that arrived in a latin1-preserved header string.
  const latin1View = Buffer.from('€-rates', 'utf8').toString('latin1');
  assert.equal(decodeRfc7578Bytes(latin1View, 'filename'), '€-rates');
});

test('decodeRfc7578Bytes rejects invalid UTF-8 with BAD_HEADER_ENCODING', () => {
  const broken = Buffer.from([0xe2, 0x82]).toString('latin1');
  assert.throws(
    () => decodeRfc7578Bytes(broken, 'filename'),
    (err: unknown) => isMultipartError(err) && err.code === 'BAD_HEADER_ENCODING',
  );
});

test('decodeExtValue decodes UTF-8 percent-encoded euro sign', () => {
  assert.equal(decodeExtValue("UTF-8''%E2%82%AC%20rates", 'filename*'), '€ rates');
});

test('decodeExtValue accepts a language tag', () => {
  assert.equal(decodeExtValue("UTF-8'en'%E2%82%AC", 'filename*'), '€');
});

test('decodeExtValue supports ISO-8859-1 payload', () => {
  assert.equal(decodeExtValue("ISO-8859-1''r%E4ksm%F6g%E5s", 'filename*'), 'räksmögås');
});

test('decodeExtValue rejects an unsupported charset', () => {
  assert.throws(
    () => decodeExtValue("UTF-16''%00%41", 'filename*'),
    (err: unknown) => isMultipartError(err) && err.code === 'BAD_HEADER_ENCODING',
  );
});

test('decodeExtValue rejects a malformed percent escape', () => {
  assert.throws(
    () => decodeExtValue("UTF-8''%E", 'filename*'),
    (err: unknown) => isMultipartError(err) && err.code === 'BAD_HEADER_ENCODING',
  );
  assert.throws(
    () => decodeExtValue("UTF-8''%ZZ", 'filename*'),
    (err: unknown) => isMultipartError(err) && err.code === 'BAD_HEADER_ENCODING',
  );
});

test('decodeExtValue rejects characters outside attr-char', () => {
  assert.throws(
    () => decodeExtValue("UTF-8''a b", 'filename*'),
    (err: unknown) => isMultipartError(err) && err.code === 'BAD_HEADER_ENCODING',
  );
});

test('decodeExtValue rejects missing delimiters and empty value', () => {
  assert.throws(
    () => decodeExtValue('UTF8nodelimiters', 'filename*'),
    (err: unknown) => isMultipartError(err) && err.code === 'BAD_HEADER_ENCODING',
  );
  assert.throws(
    () => decodeExtValue("UTF-8''", 'filename*'),
    (err: unknown) => isMultipartError(err) && err.code === 'BAD_HEADER_ENCODING',
  );
});

test('assertNoEncodedWord rejects obsolete RFC 2047 encoded-words', () => {
  assert.throws(
    () => assertNoEncodedWord('=?utf-8?b?5rWL6K+V?=.txt', 'filename'),
    (err: unknown) => isMultipartError(err) && err.code === 'BAD_HEADER_ENCODING',
  );
  assert.doesNotThrow(() => assertNoEncodedWord('plain=name.txt', 'filename'));
});
