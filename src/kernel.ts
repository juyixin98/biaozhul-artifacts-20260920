/**
 * Execution kernel for constrained RFC 6902 JSON Patch.
 *
 * The kernel is transport- and storage-free: it takes a parsed patch
 * (see contract.ts) plus a document and returns either the new document
 * with a per-step trail, or a typed failure.
 *
 * Atomicity: every call works on a deep clone of the input. Operations are
 * applied in order; each operation acts on the result of the previous one.
 * Any failure rejects with `KernelOutcome.failure` and leaves the caller's
 * input document untouched — there is no partially-mutated result.
 */

import type { PatchOperation } from './contract.js';
import {
  arrayIndex,
  locateParent,
  parsePointer,
  PointerError,
  resolvePointer,
} from './json-pointer.js';

export type KernelFailureCategory =
  | 'TEST_FAILED'
  | 'POINTER_ERROR'
  | 'MOVE_TARGET_DESCENDANT'
  | 'ROOT_LOCATION_INVALID';

export interface AppliedStep {
  index: number;
  op: PatchOperation['op'];
  path: string;
  from?: string;
  status: 'applied';
  /** Document state immediately after this operation succeeded. */
  resultAfter: unknown;
}

export interface KernelSuccess {
  ok: true;
  result: unknown;
  steps: AppliedStep[];
}

export interface KernelFailure {
  ok: false;
  category: KernelFailureCategory;
  message: string;
  failedAtIndex: number;
  /** Steps that had already succeeded before the failure. */
  appliedBeforeFailure: AppliedStep[];
  /** Always true: the caller's document was never mutated. */
  rolledBack: true;
}

export type KernelOutcome = KernelSuccess | KernelFailure;

class KernelError extends Error {
  constructor(
    readonly category: KernelFailureCategory,
    message: string,
  ) {
    super(message);
    this.name = 'KernelError';
  }
}

/** Deep equality per RFC 6902 §4.6 (same JSON type, members and values). */
export function jsonDeepEqual(a: unknown, b: unknown): boolean {
  if (a === b) {
    // Two distinct object references are never `===`; primitives are.
    return true;
  }
  if (typeof a !== typeof b || a === null || b === null) return false;
  if (Array.isArray(a)) {
    if (!Array.isArray(b) || a.length !== b.length) return false;
    return a.every((item, i) => jsonDeepEqual(item, b[i]));
  }
  if (typeof a === 'object') {
    if (typeof b !== 'object' || Array.isArray(b)) return false;
    const ka = Object.keys(a as Record<string, unknown>);
    const kb = Object.keys(b as Record<string, unknown>);
    if (ka.length !== kb.length) return false;
    return ka.every(
      (key) =>
        Object.prototype.hasOwnProperty.call(b, key) &&
        jsonDeepEqual(
          (a as Record<string, unknown>)[key],
          (b as Record<string, unknown>)[key],
        ),
    );
  }
  return false;
}

function clone<T>(value: T): T {
  // structuredClone is available on Node >=17 and refuses functions, which
  // cannot appear in JSON anyway.
  return structuredClone(value);
}

/**
 * Apply a parsed patch. Pure function: `document` is never mutated.
 */
export function applyPatch(document: unknown, operations: readonly PatchOperation[]): KernelOutcome {
  const work: { root: unknown } = { root: clone(document) };
  const steps: AppliedStep[] = [];

  const fail = (index: number, error: unknown): KernelFailure => {
    const message =
      error instanceof Error ? error.message : String(error);
    const category: KernelFailureCategory =
      error instanceof KernelError
        ? error.category
        : error instanceof PointerError
          ? 'POINTER_ERROR'
          : 'POINTER_ERROR';
    return {
      ok: false,
      category,
      message,
      failedAtIndex: index,
      appliedBeforeFailure: steps,
      rolledBack: true,
    };
  };

  for (let i = 0; i < operations.length; i++) {
    const operation = operations[i]!;
    try {
      applyOne(work, operation);
    } catch (error) {
      return fail(i, error);
    }
    steps.push({
      index: i,
      op: operation.op,
      path: operation.path,
      ...(operation.from !== undefined ? { from: operation.from } : {}),
      status: 'applied',
      resultAfter: clone(work.root),
    });
  }

  return { ok: true, result: work.root, steps };
}

/** Apply a single operation by mutating `work.root`. */
function applyOne(work: { root: unknown }, op: PatchOperation): void {
  const segments = parsePointer(op.path);

  switch (op.op) {
    case 'test': {
      let actual: unknown;
      try {
        actual = resolvePointer(work.root, op.path);
      } catch (error) {
        if (error instanceof PointerError) {
          throw new KernelError(
            'TEST_FAILED',
            `test at ${op.path} failed: target does not exist (${error.message})`,
          );
        }
        throw error;
      }
      if (!jsonDeepEqual(actual, op.value)) {
        throw new KernelError(
          'TEST_FAILED',
          `test at ${op.path} failed: actual ${summarize(actual)} ` +
            `!== expected ${summarize(op.value)}`,
        );
      }
      return;
    }

    case 'remove': {
      if (segments.length === 0) {
        throw new KernelError(
          'ROOT_LOCATION_INVALID',
          `cannot remove the whole document (path '')`,
        );
      }
      const { parent, key } = locateParent(work.root, op.path, segments);
      if (Array.isArray(parent)) {
        const index = arrayIndex(key, parent.length, op.path, segments.length - 1, {
          allowDash: false,
          allowEnd: false,
        });
        parent.splice(index, 1);
      } else {
        if (!Object.prototype.hasOwnProperty.call(parent, key)) {
          throw new PointerError(
            `cannot remove missing object member ${JSON.stringify(key)}`,
            op.path,
            segments.length - 1,
          );
        }
        delete (parent as Record<string, unknown>)[key];
      }
      return;
    }

    case 'add': {
      if (segments.length === 0) {
        work.root = clone(op.value);
        return;
      }
      const { parent, key } = locateParent(work.root, op.path, segments);
      if (Array.isArray(parent)) {
        const index = arrayIndex(key, parent.length, op.path, segments.length - 1, {
          allowDash: true,
          allowEnd: true,
        });
        parent.splice(index, 0, clone(op.value));
      } else {
        (parent as Record<string, unknown>)[key] = clone(op.value);
      }
      return;
    }

    case 'replace': {
      // Target MUST exist (RFC 6902 §4.3); root replace is allowed.
      resolvePointer(work.root, op.path);
      if (segments.length === 0) {
        work.root = clone(op.value);
        return;
      }
      const { parent, key } = locateParent(work.root, op.path, segments);
      if (Array.isArray(parent)) {
        const index = arrayIndex(key, parent.length, op.path, segments.length - 1, {
          allowDash: false,
          allowEnd: false,
        });
        parent[index] = clone(op.value);
      } else {
        (parent as Record<string, unknown>)[key] = clone(op.value);
      }
      return;
    }

    case 'copy': {
      const source = resolvePointer(work.root, op.from!);
      // copy == add(path, value-at-from) against the current document.
      applyOne(work, { op: 'add', path: op.path, value: clone(source) });
      return;
    }

    case 'move': {
      const fromSegments = parsePointer(op.from!);
      if (fromSegments.length === 0) {
        throw new KernelError(
          'ROOT_LOCATION_INVALID',
          `cannot move the whole document (from '')`,
        );
      }
      // Re-checked here (not only in the contract parser) so the kernel is
      // safe to call directly.
      if (op.from === op.path || op.path.startsWith(op.from! + '/')) {
        throw new KernelError(
          'MOVE_TARGET_DESCENDANT',
          `move destination ${op.path} is the source itself or a descendant of ${op.from}`,
        );
      }
      // RFC 6902 §4.4: remove first, then add — so moving to a later array
      // index uses post-removal positions.
      const removed = detachValue(work, op.from!, fromSegments);
      applyOne(work, { op: 'add', path: op.path, value: removed });
      return;
    }
  }
}

/** Remove and return the value at `pointer` (a detached, owned reference). */
function detachValue(
  work: { root: unknown },
  pointer: string,
  segments: string[],
): unknown {
  const { parent, key } = locateParent(work.root, pointer, segments);
  if (Array.isArray(parent)) {
    const index = arrayIndex(key, parent.length, pointer, segments.length - 1, {
      allowDash: false,
      allowEnd: false,
    });
    const [removed] = parent.splice(index, 1);
    return removed;
  }
  if (!Object.prototype.hasOwnProperty.call(parent, key)) {
    throw new PointerError(
      `cannot move/copy missing object member ${JSON.stringify(key)}`,
      pointer,
      segments.length - 1,
    );
  }
  const removed = (parent as Record<string, unknown>)[key];
  delete (parent as Record<string, unknown>)[key];
  return removed;
}

function summarize(value: unknown): string {
  const text = JSON.stringify(value);
  if (text === undefined) return String(value);
  return text.length > 120 ? text.slice(0, 117) + '...' : text;
}
