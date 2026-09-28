import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { ContractError, parsePatch, parsePatchJson } from '../src/contract.ts';

const op = (over: Record<string, unknown>) => ({ path: '/x', ...over });

describe('contract parser', () => {
  it('accepts the six supported operations with the right fields', () => {
    const { operations } = parsePatch([
      { op: 'test', path: '/a', value: 1 },
      { op: 'add', path: '/a', value: 1 },
      { op: 'remove', path: '/a' },
      { op: 'replace', path: '', value: {} },
      { op: 'move', from: '/a', path: '/b' },
      { op: 'copy', from: '/a', path: '/b' },
    ]);
    assert.equal(operations.length, 6);
  });

  it('rejects malformed JSON with MALFORMED_JSON category', () => {
    assert.throws(() => parsePatchJson('{bad'), (e: Error) => {
      assert.ok(e instanceof ContractError);
      assert.equal(e.category, 'MALFORMED_JSON');
      return true;
    });
  });

  it('rejects non-array patch documents', () => {
    try {
      parsePatch({ op: 'add' });
      assert.fail('expected ContractError');
    } catch (e) {
      normalize(e);
      assert.equal((e as ContractError).category, 'PATCH_NOT_ARRAY');
    }
  });

  it('rejects empty patch in this constrained service', () => {
    try {
      parsePatch([]);
      assert.fail('expected ContractError');
    } catch (e) {
      normalize(e);
      assert.equal((e as ContractError).category, 'EMPTY_PATCH');
    }
  });

  it('reports the failing operation index on bad op', () => {
    try {
      parsePatch([
        { op: 'add', path: '/a', value: 1 },
        { op: 'nope', path: '/a' },
      ]);
      assert.fail('expected ContractError');
    } catch (e) {
      normalize(e);
      const error = e as ContractError;
      assert.equal(error.category, 'UNKNOWN_OP');
      assert.equal(error.operationIndex, 1);
    }
  });

  it('rejects move into self / descendant at contract time (no document needed)', () => {
    for (const bad of [
      { op: 'move', from: '/a', path: '/a' },
      { op: 'move', from: '/a', path: '/a/b' },
      { op: 'move', from: '', path: '/a' },
    ]) {
      try {
        parsePatch([bad]);
        assert.fail('expected CONTRACT rejection for ' + JSON.stringify(bad));
      } catch (e) {
        normalize(e);
        assert.equal((e as ContractError).category, 'MOVE_INTO_SELF');
      }
    }
  });

  it('rejects non-string from for move/copy', () => {
    try {
      parsePatch([{ op: 'move', from: 0, path: '/b' }]);
      assert.fail('expected ContractError');
    } catch (e) {
      normalize(e);
      assert.equal((e as ContractError).category, 'MISSING_FIELD');
      assert.equal((e as ContractError).field, 'from');
    }
  });

  it('rejects add without value', () => {
    try {
      parsePatch([{ op: 'add', path: '/a' }]);
      assert.fail('expected ContractError');
    } catch (catchError) {
      normalize(catchError);
      assert.equal((catchError as ContractError).category, 'MISSING_FIELD');
      assert.equal((catchError as ContractError).field, 'value');
    }
  });

  it('rejects malformed pointers', () => {
    try {
      parsePatch([{ op: 'remove', path: 'noslash' }]);
      assert.fail('expected ContractError');
    } catch (e) {
      normalize(e);
      assert.equal((e as ContractError).category, 'MALFORMED_POINTER');
      assert.equal((e as ContractError).field, 'path');
    }
  });

  it('rejects unknown members on an operation', () => {
    try {
      parsePatch([{ op: 'remove', path: '/a', unexpected: 1 }]);
      assert.fail('expected ContractError');
    } catch (e) {
      normalize(e);
      assert.equal((e as ContractError).category, 'BAD_FIELD_TYPE');
    }
  });
});

function normalize(e: unknown): asserts e is ContractError {
  assert.ok(e instanceof ContractError, String(e));
}
