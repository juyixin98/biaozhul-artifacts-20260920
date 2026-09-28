/**
 * Tests for the contract parsing layer: media types, parameters, disposition,
 * RFC 5987 filename* decoding and the safe-basename rules. Each test asserts a
 * concrete parsed value or a concrete failure code.
 */

import { describe, expect, it } from 'vitest';
import { MultipartError } from '../src/protocol/errors.js';
import {
  decodeExtValue,
  isToken,
  parseContentDisposition,
  parseMediaType,
  parseMultipartContentType,
  parseParameters,
  safeBasename
} from '../src/protocol/header-values.js';
import { buildPartMeta, parseHeaderBlock } from '../src/protocol/part-headers.js';

function expectCode(fn: () => unknown, code: string): void {
  try {
    fn();
  } catch (err) {
    expect(err).toBeInstanceOf(MultipartError);
    expect((err as MultipartError).code).toBe(code);
    expect((err as MultipartError).errorClass).toBe('INPUT_ERROR');
    return;
  }
  throw new Error(`expected ${code}`);
}

describe('isToken', () => {
  it('accepts RFC tchars', () => {
    expect(isToken("!#$%&'*+-.^_`|~abcXYZ012")).toBe(true);
  });
  it('rejects whitespace, separators, empty', () => {
    expect(isToken('')).toBe(false);
    expect(isToken('a b')).toBe(false);
    expect(isToken('a;b')).toBe(false);
    expect(isToken('a/b')).toBe(false);
  });
});

describe('parseParameters', () => {
  it('parses quoted values containing ; and =', () => {
    const ps = parseParameters('a="x;y=z"; b=tok', 't');
    expect(ps.map((p) => [p.name, p.value, p.quoted])).toEqual([
      ['a', 'x;y=z', true],
      ['b', 'tok', false]
    ]);
  });
  it('applies quoted-pair escapes', () => {
    const ps = parseParameters('n="a\\"b\\\\c"', 't');
    expect(ps[0]!.value).toBe('a"b\\c');
  });
  it('lower-cases parameter names but not values', () => {
    const ps = parseParameters('FileName="X.TXT"', 't');
    expect(ps[0]!.name).toBe('filename');
    expect(ps[0]!.value).toBe('X.TXT');
  });
  it('rejects duplicate parameters', () => {
    expectCode(() => parseParameters('; a=1; a=2', 't'), 'MALFORMED_HEADER_SYNTAX');
  });
  it('rejects unterminated quoted value', () => {
    expectCode(() => parseParameters('; a="abc', 't'), 'MALFORMED_HEADER_SYNTAX');
  });
  it('rejects parameter without value', () => {
    expectCode(() => parseParameters('; a', 't'), 'MALFORMED_HEADER_SYNTAX');
  });
  it('rejects junk between parameters', () => {
    expectCode(() => parseParameters('; a=1 x; b=2', 't'), 'MALFORMED_HEADER_SYNTAX');
  });
});

describe('parseMediaType', () => {
  it('normalizes type/subtype', () => {
    const mt = parseMediaType('Multipart/Form-Data ; boundary=X', 'CT');
    expect(mt.essence).toBe('multipart/form-data');
    expect(mt.params.get('boundary')!.value).toBe('X');
  });
  it('rejects missing slash', () => {
    expectCode(() => parseMediaType('plain', 'CT'), 'MALFORMED_CONTENT_TYPE');
  });
});

describe('parseMultipartContentType', () => {
  it('extracts a valid boundary', () => {
    expect(parseMultipartContentType('multipart/form-data; boundary=----x').boundary).toBe('----x');
  });
  it('rejects wrong media type', () => {
    expectCode(() => parseMultipartContentType('application/json'), 'MALFORMED_CONTENT_TYPE');
  });
  it('rejects missing boundary', () => {
    expectCode(() => parseMultipartContentType('multipart/form-data'), 'MISSING_BOUNDARY');
  });
  it('rejects boundary longer than 70 chars', () => {
    expectCode(
      () => parseMultipartContentType(`multipart/form-data; boundary=${'x'.repeat(71)}`),
      'MISSING_BOUNDARY'
    );
  });
  it('rejects boundary with illegal characters', () => {
    expectCode(
      () => parseMultipartContentType('multipart/form-data; boundary="a b"'),
      'MISSING_BOUNDARY'
    );
  });
});

describe('parseContentDisposition', () => {
  it('parses plain form-data name', () => {
    const d = parseContentDisposition('form-data; name="field1"');
    expect(d.name).toBe('field1');
    expect(d.filename).toBeNull();
  });
  it('parses filename quoted with special chars', () => {
    const d = parseContentDisposition('form-data; name="f"; filename="a;b=\\"c\\""');
    expect(d.filename).toBe('a;b="c"');
  });
  it('filename* overrides filename at retrieval layer (value decoded)', () => {
    const d = parseContentDisposition("form-data; name=f; filename=\"a.txt\"; filename*=UTF-8''%C3%A4.txt");
    expect(d.filename).toBe('a.txt');
    expect(d.filenameStar).toBe('ä.txt');
    expect(d.filenameStarCharset).toBe('UTF-8');
  });
  it('rejects non-form-data disposition', () => {
    expectCode(() => parseContentDisposition('attachment; name="x"'), 'UNSUPPORTED_DISPOSITION');
  });
  it('rejects missing name', () => {
    expectCode(() => parseContentDisposition('form-data; filename="x"'), 'MISSING_DISPOSITION_NAME');
  });
  it('rejects empty name', () => {
    expectCode(() => parseContentDisposition('form-data; name=""'), 'EMPTY_FIELD_NAME');
  });
  it('rejects duplicate name parameters', () => {
    expectCode(
      () => parseContentDisposition('form-data; name="a"; name="b"'),
      'MALFORMED_HEADER_SYNTAX'
    );
  });
});

describe('decodeExtValue (RFC 5987)', () => {
  it('decodes UTF-8 percent sequences', () => {
    expect(decodeExtValue("UTF-8''%E6%96%87%E4%BB%B6.txt", 'fn').value).toBe('文件.txt');
  });
  it('preserves attr-chars unescaped', () => {
    expect(decodeExtValue("UTF-8''a-b_c.txt", 'fn').value).toBe('a-b_c.txt');
  });
  it('rejects non-UTF-8 charset', () => {
    expectCode(() => decodeExtValue("iso-8859-1''%E4", 'fn'), 'UNSUPPORTED_ENCODING');
  });
  it('rejects malformed percent escape', () => {
    expectCode(() => decodeExtValue("UTF-8''%zz", 'fn'), 'MALFORMED_FILENAME_ENCODING');
    expectCode(() => decodeExtValue("UTF-8''%4", 'fn'), 'MALFORMED_FILENAME_ENCODING');
  });
  it('rejects illegal attr char', () => {
    expectCode(() => decodeExtValue("UTF-8''a b", 'fn'), 'MALFORMED_FILENAME_ENCODING');
  });
  it('rejects invalid UTF-8 byte sequence', () => {
    expectCode(() => decodeExtValue("UTF-8''%ff", 'fn'), 'MALFORMED_FILENAME_ENCODING');
  });
  it('rejects missing delimiters', () => {
    expectCode(() => decodeExtValue('UTF-8abc', 'fn'), 'MALFORMED_FILENAME_ENCODING');
  });
});

describe('safeBasename', () => {
  it('accepts a plain basename', () => {
    expect(safeBasename('photo.png', 'fn')).toBe('photo.png');
  });
  it('rejects path separators and traversal', () => {
    expectCode(() => safeBasename('../../etc/passwd', 'fn'), 'PATH_TRAVERSAL_FILENAME');
    expectCode(() => safeBasename('a/b', 'fn'), 'PATH_TRAVERSAL_FILENAME');
    expectCode(() => safeBasename('a\\b', 'fn'), 'PATH_TRAVERSAL_FILENAME');
    expectCode(() => safeBasename('..', 'fn'), 'PATH_TRAVERSAL_FILENAME');
    expectCode(() => safeBasename('x..y', 'fn'), 'PATH_TRAVERSAL_FILENAME');
  });
  it('rejects control chars and NUL', () => {
    expectCode(() => safeBasename('a\nb', 'fn'), 'PATH_TRAVERSAL_FILENAME');
    expectCode(() => safeBasename('a\x00b', 'fn'), 'PATH_TRAVERSAL_FILENAME');
  });
  it('rejects surrounding whitespace and trailing dot', () => {
    expectCode(() => safeBasename(' a', 'fn'), 'PATH_TRAVERSAL_FILENAME');
    expectCode(() => safeBasename('a.', 'fn'), 'PATH_TRAVERSAL_FILENAME');
  });
});

describe('header block parsing', () => {
  const enc = (s: string): Buffer => Buffer.from(s, 'utf8');

  it('parses a well-formed block into metadata', () => {
    const meta = buildPartMeta(
      enc('Content-Disposition: form-data; name="f"; filename="x.txt"\r\nContent-Type: text/plain'),
      3
    );
    expect(meta.isFile).toBe(true);
    expect(meta.name).toBe('f');
    expect(meta.effectiveFilename).toBe('x.txt');
    expect(meta.contentType).toBe('text/plain');
    expect(meta.index).toBe(3);
  });
  it('rejects duplicate Content-Disposition', () => {
    expectCode(
      () =>
        parseHeaderBlock(
          enc('Content-Disposition: form-data; name="a"\r\nContent-Disposition: form-data; name="b"')
        ),
      'DUPLICATE_CONTENT_DISPOSITION'
    );
  });
  it('rejects duplicate Content-Type', () => {
    expectCode(
      () =>
        parseHeaderBlock(
          enc('Content-Disposition: form-data; name="a"\r\nContent-Type: text/plain\r\nContent-Type: image/png')
        ),
      'DUPLICATE_CONTENT_TYPE'
    );
  });
  it('rejects missing disposition', () => {
    expectCode(() => parseHeaderBlock(enc('Content-Type: text/plain')), 'MALFORMED_PART_HEADERS');
  });
  it('rejects folded header line', () => {
    expectCode(
      () => parseHeaderBlock(enc('Content-Disposition: form-data; name="a"\r\n continuation')),
      'MALFORMED_HEADER_SYNTAX'
    );
  });
  it('rejects non-token header name', () => {
    expectCode(() => parseHeaderBlock(enc('Bad Header: x')), 'HEADER_NAME_NOT_TOKEN');
    expectCode(
      () => parseHeaderBlock(enc('Content-Disposition: form-data; name="a"\r\nX Y: z')),
      'HEADER_NAME_NOT_TOKEN'
    );
  });
  it('rejects invalid UTF-8 in the header block', () => {
    const raw = Buffer.concat([Buffer.from('Content-Disposition: form-data; name="', 'latin1'), Buffer.from([0xff]), Buffer.from('"')]);
    expectCode(() => parseHeaderBlock(raw), 'MALFORMED_PART_HEADERS');
  });
});
