import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseMultipartContentType } from '../../src/protocol/content-type.js';
import { parseHeaderParameters } from '../../src/protocol/params.js';
import { DEFAULT_LIMITS } from '../../src/protocol/config.js';
import { isMultipartError } from '../../src/protocol/errors.js';

test('accepts a well-formed multipart/form-data boundary', () => {
  const env = parseMultipartContentType(
    'multipart/form-data; boundary=----WebKitFormBoundaryABC',
    DEFAULT_LIMITS,
  );
  assert.equal(env.boundary, '----WebKitFormBoundaryABC');
  assert.equal(env.boundaryDelimiter, '------WebKitFormBoundaryABC');
});

test('accepts a quoted boundary containing otherwise-special chars', () => {
  const env = parseMultipartContentType(
    'multipart/form-data; boundary="b(o)u+n_d,ry?"',
    DEFAULT_LIMITS,
  );
  assert.equal(env.boundary, 'b(o)u+n_d,ry?');
});

test('rejects a missing Content-Type header as NOT_MULTIPART', () => {
  assert.throws(
    () => parseMultipartContentType(undefined, DEFAULT_LIMITS),
    (e: unknown) => isMultipartError(e) && e.code === 'NOT_MULTIPART',
  );
});

test('rejects a non-multipart media type as NOT_MULTIPART', () => {
  assert.throws(
    () => parseMultipartContentType('application/json', DEFAULT_LIMITS),
    (e: unknown) => isMultipartError(e) && e.errorClass === 'INPUT_ERROR' && e.code === 'NOT_MULTIPART',
  );
});

test('rejects a missing boundary parameter as MISSING_BOUNDARY', () => {
  assert.throws(
    () => parseMultipartContentType('multipart/form-data', DEFAULT_LIMITS),
    (e: unknown) => isMultipartError(e) && e.code === 'MISSING_BOUNDARY',
  );
});

test('rejects an over-long boundary as BOUNDARY_TOO_LONG (RESOURCE_LIMIT)', () => {
  const long = 'x'.repeat(DEFAULT_LIMITS.maxBoundaryLength + 1);
  assert.throws(
    () => parseMultipartContentType(`multipart/form-data; boundary=${long}`, DEFAULT_LIMITS),
    (e: unknown) =>
      isMultipartError(e) && e.code === 'BOUNDARY_TOO_LONG' && e.errorClass === 'RESOURCE_LIMIT',
  );
});

test('rejects a boundary with a space (outside bchars)', () => {
  assert.throws(
    () => parseMultipartContentType('multipart/form-data; boundary="a b"', DEFAULT_LIMITS),
    (e: unknown) => isMultipartError(e) && e.code === 'MISSING_BOUNDARY',
  );
});

test('parameter parser keeps semicolons inside quoted strings literal', () => {
  const parsed = parseHeaderParameters(
    'form-data; name="a;b;c"; filename="x.txt"',
    'Content-Disposition',
  );
  assert.equal(parsed.value, 'form-data');
  assert.equal(parsed.params.get('name'), 'a;b;c');
  assert.equal(parsed.params.get('filename'), 'x.txt');
});

test('parameter parser honours quoted-pair escapes', () => {
  const parsed = parseHeaderParameters(
    'form-data; name="q\\"x\\\\y"',
    'Content-Disposition',
  );
  assert.equal(parsed.params.get('name'), 'q"x\\y');
});

test('parameter parser rejects a duplicated parameter as DUPLICATE_HEADER', () => {
  assert.throws(
    () =>
      parseHeaderParameters(
        'form-data; name="a"; name="b"',
        'Content-Disposition',
      ),
    (e: unknown) => isMultipartError(e) && e.code === 'DUPLICATE_HEADER',
  );
});

test('parameter parser rejects a parameter without "="', () => {
  assert.throws(
    () => parseHeaderParameters('form-data; bogus', 'Content-Disposition'),
    (e: unknown) => isMultipartError(e) && e.code === 'MALFORMED_HEADER',
  );
});

test('parameter parser rejects an unterminated quoted string', () => {
  assert.throws(
    () => parseHeaderParameters('form-data; name="abc', 'Content-Disposition'),
    (e: unknown) => isMultipartError(e) && e.code === 'MALFORMED_HEADER',
  );
});
