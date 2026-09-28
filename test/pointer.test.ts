import { describe, expect, it } from 'vitest';
import { PatchError } from '../src/errors';
import {
  escapeToken,
  jsonTypeName,
  parseArrayIndex,
  parsePointer,
  resolve,
} from '../src/pointer';

describe('parsePointer — tokenization and escape semantics', () => {
  it('parses the empty pointer as zero tokens (whole document)', () => {
    expect(parsePointer('').tokens).toEqual([]);
    expect(parsePointer('').raw).toBe('');
  });

  it('splits on slashes and preserves empty reference tokens', () => {
    const p = parsePointer('/a//b/');
    expect([...p.tokens]).toEqual(['a', '', 'b', '']);
  });

  it('decodes ~1 to slash before ~0 to tilde (RFC 6901 ordering)', () => {
    expect([...parsePointer('/a~1b').tokens]).toEqual(['a/b']);
    expect([...parsePointer('/c~0d').tokens]).toEqual(['c~d']);
    expect([...parsePointer('/e~01f').tokens]).toEqual(['e~1f']);
    expect([...parsePointer('/g~001h').tokens]).toEqual(['g~01h']);
  });

  it('round-trips tokens containing slash and tilde through escape/parse', () => {
    for (const token of ['', 'a/b', 'p~q', '~', '/', 'x~1/y~0']) {
      const roundTrip = parsePointer(`/${escapeToken(token)}`).tokens[0];
      expect(roundTrip).toBe(token);
    }
  });

  it('rejects pointers not starting with slash', () => {
    expect(() => parsePointer('a/b')).toThrow(PatchError);
    try {
      parsePointer('a/b');
      throw new Error('should have thrown');
    } catch (err) {
      expect(err).toBeInstanceOf(PatchError);
      expect((err as PatchError).category).toBe('INVALID_POINTER');
    }
  });

  it('rejects dangling or illegal tilde escapes with INVALID_POINTER', () => {
    for (const bad of ['/~', '/~2', '/a~', '/x~y']) {
      try {
        parsePointer(bad);
        throw new Error(`should have thrown for ${bad}`);
      } catch (err) {
        expect(err).toBeInstanceOf(PatchError);
        expect((err as PatchError).category).toBe('INVALID_POINTER');
      }
    }
  });

  it('rejects non-string pointer input', () => {
    expect(() => parsePointer(42)).toThrow(PatchError);
    expect(() => parsePointer(null)).toThrow(PatchError);
  });
});

describe('parseArrayIndex — RFC 6902 array reference rules', () => {
  it('accepts digits and the append token', () => {
    expect(parseArrayIndex('0')).toBe(0);
    expect(parseArrayIndex('12')).toBe(12);
    expect(parseArrayIndex('-')).toBe(-1);
  });

  it('rejects leading zeros, non-digits, and signs', () => {
    for (const bad of ['01', '00', '+1', '-1', ' 1', '1 ', '1.0', '']) {
      try {
        parseArrayIndex(bad);
        throw new Error(`should reject ${JSON.stringify(bad)}`);
      } catch (err) {
        expect((err as PatchError).category).toBe('ARRAY_INDEX_INVALID');
      }
    }
  });
});

describe('resolve — addressing and concrete failure categories', () => {
  const doc = {
    list: [{ id: 'first' }, { id: 'second' }],
    'a/b': 7,
    nested: { deep: { leaf: null } },
    scalar: 3,
  };

  it('resolves nested object keys with escaped slashes', () => {
    expect(resolve(doc, parsePointer('/a~1b'))).toBe(7);
  });

  it('resolves array elements by position', () => {
    expect(resolve(doc, parsePointer('/list/1/id'))).toBe('second');
  });

  it('resolves the root document', () => {
    expect(resolve(doc, parsePointer(''))).toBe(doc);
  });

  it('reports POINTER_TARGET_MISSING for absent object key with the key in details', () => {
    try {
      resolve(doc, parsePointer('/nope'));
      throw new Error('expected throw');
    } catch (err) {
      expect((err as PatchError).category).toBe('POINTER_TARGET_MISSING');
      expect((err as PatchError).details).toMatchObject({ key: 'nope' });
    }
  });

  it('reports POINTER_TARGET_MISSING for out-of-range and "-" read positions', () => {
    for (const path of ['/list/2', '/list/-']) {
      try {
        resolve(doc, parsePointer(path));
        throw new Error(`expected throw for ${path}`);
      } catch (err) {
        expect((err as PatchError).category).toBe('POINTER_TARGET_MISSING');
      }
    }
  });

  it('reports PATH_TYPE_MISMATCH when traversing a scalar', () => {
    try {
      resolve(doc, parsePointer('/scalar/x'));
      throw new Error('expected throw');
    } catch (err) {
      expect((err as PatchError).category).toBe('PATH_TYPE_MISMATCH');
      expect((err as PatchError).details).toMatchObject({ actualType: 'number' });
    }
  });

  it('reports ARRAY_INDEX_INVALID for a non-numeric array token', () => {
    try {
      resolve(doc, parsePointer('/list/x'));
      throw new Error('expected throw');
    } catch (err) {
      expect((err as PatchError).category).toBe('ARRAY_INDEX_INVALID');
    }
  });

  it('exposes json type names', () => {
    expect(jsonTypeName(null)).toBe('null');
    expect(jsonTypeName([])).toBe('array');
    expect(jsonTypeName(1)).toBe('number');
    expect(jsonTypeName('s')).toBe('string');
  });
});
