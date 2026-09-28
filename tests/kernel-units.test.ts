import { describe, expect, it } from 'vitest';
import { duplicateKey, newBatchId, newConnectionId, newOperationId, renderId } from '../src/kernel/ids.js';
import { TaskRegistry } from '../src/kernel/task-registry.js';
import {
  booleanField,
  integerField,
  rejectUnknownFields,
  requireArray,
  requireObject,
  stringField,
} from '../src/kernel/methods/validate.js';
import { ErrorCode, type JsonValue } from '../src/contract/protocol.js';
import { MethodError } from '../src/kernel/method.js';
import { stableFingerprint } from '../src/kernel/methods/index.js';
import { redact } from '../src/diagnostics/redact.js';

describe('ids: type-aware duplicate keys and rendering', () => {
  it('separates number, string and null ids', () => {
    expect(duplicateKey(1)).toBe(duplicateKey(1));
    expect(duplicateKey(1)).not.toBe(duplicateKey('1'));
    expect(duplicateKey(0)).toBe(duplicateKey(-0));
    expect(duplicateKey(null)).toBe('n:null');
    expect(duplicateKey('a')).not.toBe(duplicateKey('b'));
  });

  it('generates unique, prefixed correlation ids', () => {
    expect(newOperationId().startsWith('op_')).toBe(true);
    expect(newBatchId().startsWith('batch_')).toBe(true);
    expect(newConnectionId().startsWith('conn_')).toBe(true);
    const ids = new Set(Array.from({ length: 50 }, () => newOperationId()));
    expect(ids.size).toBe(50);
  });

  it('renders notifications distinctly from id null', () => {
    expect(renderId(undefined)).toBe('<notification>');
    expect(renderId(null)).toBe('null');
    expect(renderId(7)).toBe('7');
    expect(renderId('x')).toBe('"x"');
  });
});

describe('fingerprint: canonical, type-sensitive', () => {
  it('ignores key order but preserves value types', () => {
    expect(stableFingerprint({ a: 1, b: 2 })).toBe(stableFingerprint({ b: 2, a: 1 }));
    expect(stableFingerprint({ a: 1 })).not.toBe(stableFingerprint({ a: '1' }));
    expect(stableFingerprint([1, 2])).not.toBe(stableFingerprint([2, 1]));
    expect(stableFingerprint(undefined)).toBe(stableFingerprint(null));
  });
});

describe('TaskRegistry: registration, cancellation, draining', () => {
  it('tracks liveness and removes settled tasks', async () => {
    const reg = new TaskRegistry();
    const controller = new AbortController();
    let resolveWork: (v: unknown) => void = () => undefined;
    const work = new Promise((resolve) => {
      resolveWork = resolve;
    });
    const task = reg.register('op-x', work, controller, 1);
    expect(reg.isLive('op-x')).toBe(true);
    expect(reg.liveCount()).toBe(1);
    expect(reg.get('op-x')).toBe(task);

    let cancelled = false;
    controller.signal.addEventListener('abort', () => {
      cancelled = true;
    });
    expect(task.cancel('stop')).toBeUndefined();
    expect(cancelled).toBe(true);

    resolveWork('done');
    await task.done;
    expect(reg.isLive('op-x')).toBe(false);
    expect(reg.liveCount()).toBe(0);
  });

  it('cancel on unknown task is a no-op returning undefined from get', () => {
    const reg = new TaskRegistry();
    expect(reg.get('nope')).toBeUndefined();
    expect(reg.isLive('nope')).toBe(false);
  });

  it('drain resolves immediately when nothing is live', async () => {
    const reg = new TaskRegistry();
    await reg.drain(10);
    expect(reg.liveCount()).toBe(0);
  });

  it('drain waits for in-flight tasks (and tolerates rejections)', async () => {
    const reg = new TaskRegistry();
    let rejectWork: (e: Error) => void = () => undefined;
    const work = new Promise((_resolve, reject) => {
      rejectWork = reject;
    });
    const controller = new AbortController();
    const task = reg.register('op-fail', work, controller, 1);
    setTimeout(() => rejectWork(new Error('boom')), 5);
    await reg.drain(1000);
    await task.done;
    expect(reg.isLive('op-fail')).toBe(false);
  });

  it('drain returns after the timeout even if a task never settles', async () => {
    const reg = new TaskRegistry();
    // A promise that never resolves; drain must not hang past the timeout.
    const controller = new AbortController();
    reg.register('op-hang', new Promise(() => undefined), controller, 1);
    const start = Date.now();
    await reg.drain(10);
    expect(Date.now() - start).toBeLessThan(200);
    expect(reg.liveCount()).toBe(1);
  });
});

describe('strict validators: reject silent coercion', () => {
  const obj = (v: Record<string, JsonValue>) => v;

  it('requireObject / requireArray enforce shape', () => {
    expect(() => requireObject([1], 'm')).toThrow(MethodError);
    expect(() => requireObject(null, 'm')).toThrow(MethodError);
    expect(() => requireArray({ a: 1 }, 'm')).toThrow(MethodError);
    expect(requireArray([1], 'm')).toEqual([1]);
  });

  it('integerField rejects booleans, floats, strings and out-of-range', () => {
    expect(() => integerField(obj({ n: true }), 'n', {})).toThrow(/integer/);
    expect(() => integerField(obj({ n: 1.5 }), 'n', {})).toThrow(/integer/);
    expect(() => integerField(obj({ n: '5' }), 'n', {})).toThrow(/integer/);
    expect(() => integerField(obj({ n: 5 }), 'n', { max: 4 })).toThrow(/<=/);
    expect(() => integerField(obj({ n: 5 }), 'n', { min: 6 })).toThrow(/>=/);
    expect(() => integerField(obj({}), 'n', {})).toThrow(/required/);
    expect(integerField(obj({}), 'n', { default: 3 })).toBe(3);
    expect(integerField(obj({ n: 7 }), 'n', { optional: true })).toBe(7);
    expect(integerField(obj({}), 'n', { optional: true })).toBeUndefined();
  });

  it('stringField enforces presence and length', () => {
    expect(() => stringField(obj({ s: 1 }), 's', {})).toThrow(/string/);
    expect(() => stringField(obj({}), 's', {})).toThrow(/required/);
    expect(() => stringField(obj({ s: '' }), 's', { min: 1 })).toThrow(/at least/);
    expect(() => stringField(obj({ s: 'abcd' }), 's', { max: 3 })).toThrow(/at most/);
    expect(stringField(obj({}), 's', { optional: true })).toBeUndefined();
    expect(stringField(obj({ s: 'ok' }), 's', {})).toBe('ok');
  });

  it('booleanField only accepts booleans', () => {
    expect(() => booleanField(obj({ b: 1 }), 'b')).toThrow(/boolean/);
    expect(booleanField(obj({ b: false }), 'b')).toBe(false);
    expect(booleanField(obj({}), 'b', true)).toBeUndefined();
  });

  it('rejectUnknownFields reports allowed set', () => {
    try {
      rejectUnknownFields(obj({ good: 1, bad: 2 }), ['good']);
      throw new Error('should have thrown');
    } catch (err) {
      expect(err).toBeInstanceOf(MethodError);
      expect((err as MethodError).code).toBe(ErrorCode.INVALID_PARAMS);
      expect((err as MethodError).data).toEqual({ allowed: ['good'] });
    }
  });
});

describe('redact: non-JSON and edge values', () => {
  it('handles null, primitives, circular refs and truncates long strings', () => {
    expect(redact(null)).toBeNull();
    expect(redact(42)).toBe(42);
    expect(redact(true)).toBe(true);
    const circular: Record<string, unknown> = { a: 1 };
    circular.self = circular;
    const out = redact(circular) as Record<string, unknown>;
    expect(out.self).toBe('[Circular]');
    const long = redact('x'.repeat(700)) as string;
    expect(long.length).toBeLessThan(700);
    expect(long).toContain('more chars');
  });
});
