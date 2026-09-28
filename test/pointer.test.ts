import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import {
  arrayIndex,
  formatPointer,
  isEqualOrPrefix,
  locateParent,
  parsePointer,
  PointerError,
  resolvePointer,
} from '../src/json-pointer.ts';

describe('parsePointer / formatPointer (RFC 6901)', () => {
  it('empty pointer is zero segments', () => {
    assert.deepEqual(parsePointer(''), []);
    assert.equal(formatPointer([]), '');
  });

  it('splits segments and decodes ~1 then ~0 in the correct order', () => {
    assert.deepEqual(parsePointer('/a/b'), ['a', 'b']);
    assert.deepEqual(parsePointer('/a~1b'), ['a/b']);
    assert.deepEqual(parsePointer('/a~0b'), ['a~b']);
    // '~01' decodes to '~1' (tilde then one), not '/': ~0 is applied first.
    assert.deepEqual(parsePointer('/a~01b'), ['a~1b']);
    assert.deepEqual(parsePointer('/~0~1'), ['~/']);
  });

  it('round-trips tricky keys', () => {
    for (const key of ['a/b', 'a~b', 'a~1b', '', 'x/y~z', '~']) {
      const pointer = formatPointer([key]);
      assert.deepEqual(parsePointer(pointer), [key]);
    }
  });

  it('rejects non-empty pointers not starting with slash', () => {
    assert.throws(() => parsePointer('a/b'), PointerError);
  });

  it('empty segment is the empty key, not a parse error', () => {
    assert.deepEqual(parsePointer('/'), ['']);
    assert.deepEqual(parsePointer('//'), ['', '']);
  });
});

describe('resolvePointer', () => {
  const doc = {
    foo: ['bar', 'baz'],
    '': 0,
    'a/b': 1,
    'a~b': 2,
    nested: { deep: { list: [10, 20] } },
  };

  it('resolves the RFC 6901 example pointers', () => {
    assert.equal(resolvePointer(doc, ''), doc);
    assert.deepEqual(resolvePointer(doc, '/foo'), ['bar', 'baz']);
    assert.equal(resolvePointer(doc, '/foo/0'), 'bar');
    assert.equal(resolvePointer(doc, '/'), 0);
    assert.equal(resolvePointer(doc, '/a~1b'), 1);
    assert.equal(resolvePointer(doc, '/a~0b'), 2);
    assert.equal(resolvePointer(doc, '/nested/deep/list/1'), 20);
  });

  it('fails on missing object member', () => {
    assert.throws(() => resolvePointer(doc, '/missing'), (e: Error) => {
      assert.ok(e instanceof PointerError);
      assert.match(e.message, /no member/);
      return true;
    });
  });

  it('fails when traversing through a scalar', () => {
    assert.throws(() => resolvePointer({ a: 1 }, '/a/x'), PointerError);
  });

  it('treats unescaped slash as segment boundary (misses a/b key)', () => {
    assert.throws(() => resolvePointer({ 'a/b': 1 }, '/a/b'), PointerError);
  });
});

describe('locateParent', () => {
  it('returns the parent container and final key', () => {
    const doc = { a: [1, 2] };
    const located = locateParent(doc, '/a/1');
    assert.equal(located.parent, doc.a);
    assert.equal(located.key, '1');
  });

  it('rejects the empty pointer (no parent)', () => {
    assert.throws(() => locateParent({}, ''), PointerError);
  });
});

describe('arrayIndex', () => {
  it('accepts canonical decimal indices in range', () => {
    assert.equal(arrayIndex('0', 3, '/0', 0, { allowDash: false, allowEnd: false }), 0);
    assert.equal(arrayIndex('2', 3, '/2', 0, { allowDash: false, allowEnd: false }), 2);
  });

  it('rejects leading zeros, minus-one and non-numeric tokens on read', () => {
    assert.throws(() => arrayIndex('01', 3, '/01', 0, { allowDash: false, allowEnd: false }), PointerError);
    assert.throws(() => arrayIndex('-', 3, '/-', 0, { allowDash: false, allowEnd: false }), PointerError);
    assert.throws(() => arrayIndex('x', 3, '/x', 0, { allowDash: false, allowEnd: false }), PointerError);
  });

  it('rejects out-of-range indices on read but allows length boundary on add', () => {
    assert.throws(() => arrayIndex('3', 3, '/3', 0, { allowDash: false, allowEnd: false }), PointerError);
    assert.equal(arrayIndex('3', 3, '/3', 0, { allowDash: true, allowEnd: true }), 3);
    assert.equal(arrayIndex('-', 3, '/-', 0, { allowDash: true, allowEnd: true }), 3);
    assert.throws(() => arrayIndex('4', 3, '/4', 0, { allowDash: true, allowEnd: true }), PointerError);
  });
});

describe('isEqualOrPrefix (move ancestry rule)', () => {
  it('is true for equality and strict ancestors', () => {
    assert.equal(isEqualOrPrefix('/a', '/a'), true);
    assert.equal(isEqualOrPrefix('/a', '/a/b'), true);
    assert.equal(isEqualOrPrefix('', '/x'), true);
  });

  it('is false for siblings and mere string-prefix lookalikes', () => {
    assert.equal(isEqualOrPrefix('/a', '/b'), false);
    assert.equal(isEqualOrPrefix('/ab', '/abx'), false);
    assert.equal(isEqualOrPrefix('/a', '/b/a'), false);
  });
});
