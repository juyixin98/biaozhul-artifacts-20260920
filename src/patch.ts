/**
 * Patch kernel: RFC 6902 restricted contract parsing + atomic execution.
 *
 * The kernel is pure and side-effect free:
 *   - the input document is never mutated (structural edits share unchanged
 *     branches with their predecessor);
 *   - every operation is applied to the result of the previous one;
 *   - any failure — malformed operation, pointer error, or a failing "test" —
 *     aborts the whole patch and the returned document is the original input;
 *   - every step records the exact document state observed/produced, so
 *     atomicity and per-step evolution are externally verifiable.
 */

import { jsonEqual, type JsonValue } from './equality';
import { PatchError } from './errors';
import {
  parsePointer,
  parseArrayIndex,
  resolve,
  type JsonPointer,
} from './pointer';

export const ALLOWED_OPS = ['test', 'add', 'remove', 'replace', 'move', 'copy'] as const;
export type OpName = (typeof ALLOWED_OPS)[number];

/** Raw operation as received on the wire (already JSON-decoded). */
export interface RawOperation {
  op?: unknown;
  path?: unknown;
  value?: unknown;
  from?: unknown;
  [extra: string]: unknown;
}

interface ResolvedOperation {
  readonly index: number;
  readonly name: OpName;
  readonly path: JsonPointer;
  readonly from: JsonPointer | null;
  readonly hasValue: boolean;
  readonly value: JsonValue;
}

export type StepStatus = 'applied' | 'failed' | 'skipped';
export type DisplayedOp = OpName | '<invalid>';

export interface StepTrace {
  readonly index: number;
  readonly op: DisplayedOp;
  readonly path: string;
  readonly from?: string;
  readonly status: StepStatus;
  /** Document state immediately AFTER this applied step (shared, immutable). */
  readonly documentAfter?: JsonValue;
  /** Document state while at this step when it failed (= pre-step state). */
  readonly documentAt?: JsonValue;
  readonly error?: {
    readonly category: string;
    readonly message: string;
    readonly details: Record<string, unknown>;
  };
}

export interface PatchSuccess {
  readonly ok: true;
  readonly original: JsonValue;
  readonly result: JsonValue;
  readonly applied: number;
  readonly traces: readonly StepTrace[];
}

export interface PatchFailure {
  readonly ok: false;
  readonly original: JsonValue;
  /** On failure the result IS the original document reference: nothing stuck. */
  readonly result: JsonValue;
  readonly failedAtIndex: number;
  readonly category: string;
  readonly message: string;
  readonly details: Record<string, unknown>;
  readonly rolledBack: true;
  readonly traces: readonly StepTrace[];
}

export type PatchOutcome = PatchSuccess | PatchFailure;

const KNOWN_MEMBERS = new Set(['op', 'path', 'value', 'from']);

/**
 * Parse and validate the patch document without touching any data document.
 * Raises PatchError(MALFORMED_PATCH / INVALID_POINTER / ...) on bad contracts.
 */
export function parsePatch(raw: unknown): ResolvedOperation[] {
  if (!Array.isArray(raw)) {
    throw new PatchError('MALFORMED_PATCH', 'Patch document must be a JSON array of operations', null, {
      receivedType: Array.isArray(raw) ? 'array' : typeof raw,
    });
  }
  return raw.map((entry, index) => parseOperation(entry, index));
}

function parseOperation(entry: unknown, index: number): ResolvedOperation {
  if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) {
    throw new PatchError('MALFORMED_PATCH', 'Each patch operation must be a JSON object', index, {
      receivedType: entry === null ? 'null' : Array.isArray(entry) ? 'array' : typeof entry,
    });
  }
  const obj = entry as RawOperation;

  for (const member of Object.keys(obj)) {
    if (!KNOWN_MEMBERS.has(member)) {
      throw new PatchError(
        'MALFORMED_PATCH',
        `Unknown operation member ${JSON.stringify(member)}; allowed members are op, path, value, from`,
        index,
        { member },
      );
    }
  }

  const name = obj.op;
  if (typeof name !== 'string' || !ALLOWED_OPS.includes(name as OpName)) {
    throw new PatchError(
      'MALFORMED_PATCH',
      `Unsupported or missing "op": expected one of ${ALLOWED_OPS.join(', ')}`,
      index,
      { received: String(name) },
    );
  }
  const opName = name as OpName;

  if (typeof obj.path !== 'string') {
    throw new PatchError('MALFORMED_PATCH', 'Operation member "path" is required and must be a string', index, {
      receivedPath: obj.path,
    });
  }
  const path = parsePointer(obj.path);

  let from: JsonPointer | null = null;
  if (opName === 'move' || opName === 'copy') {
    if (typeof obj.from !== 'string') {
      throw new PatchError('MALFORMED_PATCH', `Operation "${opName}" requires a string "from" member`, index, {
        receivedFrom: obj.from,
      });
    }
    from = parsePointer(obj.from);
  } else if (obj.from !== undefined) {
    throw new PatchError('MALFORMED_PATCH', `Operation "${opName}" must not carry a "from" member`, index, {});
  }

  let hasValue = false;
  let value: JsonValue = null;
  if (opName === 'test' || opName === 'add' || opName === 'replace') {
    if (!('value' in obj)) {
      throw new PatchError('MALFORMED_PATCH', `Operation "${opName}" requires a "value" member`, index, {});
    }
    assertJsonValue(obj.value, index, 'value');
    hasValue = true;
    value = obj.value as JsonValue;
  } else if ('value' in obj) {
    throw new PatchError('MALFORMED_PATCH', `Operation "${opName}" must not carry a "value" member`, index, {});
  }

  return { index, name: opName, path, from, hasValue, value };
}

function assertJsonValue(v: unknown, opIndex: number, where: string): void {
  if (v === null) return;
  const t = typeof v;
  if (t === 'string' || t === 'boolean' || t === 'number') {
    if (t === 'number' && !Number.isFinite(v as number)) {
      throw new PatchError('MALFORMED_PATCH', `${where} must be finite JSON number`, opIndex, {});
    }
    return;
  }
  if (Array.isArray(v)) {
    v.forEach((child) => assertJsonValue(child, opIndex, where));
    return;
  }
  if (t === 'object') {
    for (const key of Object.keys(v as Record<string, unknown>)) {
      assertJsonValue((v as Record<string, unknown>)[key], opIndex, where);
    }
    return;
  }
  throw new PatchError('MALFORMED_PATCH', `${where} must be a JSON value`, opIndex, { receivedType: t });
}

/** Apply a parsed (or raw) patch. Never throws: all failures are returned. */
export function applyPatch(document: JsonValue, rawOps: unknown): PatchOutcome {
  const original = document;
  if (!Array.isArray(rawOps)) {
    return asFailure(
      original,
      new PatchError('MALFORMED_PATCH', 'Patch document must be a JSON array of operations', -1, {
        receivedType: typeof rawOps,
      }),
      -1,
      [],
    );
  }

  const traces: StepTrace[] = [];
  let current = document;

  for (let index = 0; index < rawOps.length; index += 1) {
    let op: ResolvedOperation;
    try {
      // Parse at evaluation time so failure index matches execution order.
      op = parseOperation(rawOps[index], index);
    } catch (err) {
      const pe = asPatchError(err, index);
      const failedEntry = rawOps[index] as RawOperation | undefined;
      traces.push({
        index,
        op:
          typeof failedEntry?.op === 'string' && ALLOWED_OPS.includes(failedEntry.op as OpName)
            ? (failedEntry.op as OpName)
            : '<invalid>',
        path: typeof failedEntry?.path === 'string' ? failedEntry.path : '',
        ...(typeof failedEntry?.from === 'string' ? { from: failedEntry.from } : {}),
        status: 'failed',
        documentAt: current,
        error: { category: pe.category, message: pe.message, details: pe.details },
      });
      appendSkipped(rawOps, index + 1, traces);
      return asFailure(original, pe, index, traces);
    }

    try {
      current = executeOperation(current, op);
      traces.push({
        index: op.index,
        op: op.name,
        path: op.path.raw,
        ...(op.from ? { from: op.from.raw } : {}),
        status: 'applied',
        documentAfter: current,
      });
    } catch (err) {
      const pe = asPatchError(err, op.index);
      traces.push({
        index: op.index,
        op: op.name,
        path: op.path.raw,
        ...(op.from ? { from: op.from.raw } : {}),
        status: 'failed',
        documentAt: current,
        error: { category: pe.category, message: pe.message, details: pe.details },
      });
      appendSkipped(rawOps, index + 1, traces);
      return asFailure(original, pe, op.index, traces);
    }
  }

  return {
    ok: true,
    original,
    result: current,
    applied: rawOps.length,
    traces: Object.freeze(traces),
  };
}

function appendSkipped(rawOps: unknown[], fromIndex: number, traces: StepTrace[]): void {
  for (let i = fromIndex; i < rawOps.length; i += 1) {
    const entry = rawOps[i] as RawOperation | undefined;
    traces.push({
      index: i,
      op:
        entry && typeof entry.op === 'string' && ALLOWED_OPS.includes(entry.op as OpName)
          ? (entry.op as OpName)
          : '<invalid>',
      path: typeof entry?.path === 'string' ? entry.path : '',
      ...(typeof entry?.from === 'string' ? { from: entry.from } : {}),
      status: 'skipped',
    });
  }
}

function asPatchError(err: unknown, fallbackIndex: number): PatchError {
  return err instanceof PatchError
    ? err
    : new PatchError('MALFORMED_PATCH', err instanceof Error ? err.message : String(err), fallbackIndex);
}

function asFailure(
  original: JsonValue,
  err: unknown,
  failedAtIndex: number,
  traces: StepTrace[],
): PatchFailure {
  const pe =
    err instanceof PatchError
      ? err
      : new PatchError('MALFORMED_PATCH', err instanceof Error ? err.message : String(err), failedAtIndex);
  return {
    ok: false,
    original,
    result: original,
    failedAtIndex: pe.opIndex ?? failedAtIndex,
    category: pe.category,
    message: pe.message,
    details: pe.details,
    rolledBack: true,
    traces: Object.freeze([...traces]),
  };
}

function executeOperation(doc: JsonValue, op: ResolvedOperation): JsonValue {
  switch (op.name) {
    case 'test':
      return execTest(doc, op);
    case 'remove':
      return execRemove(doc, op);
    case 'add':
      return execAdd(doc, op);
    case 'replace':
      return execReplace(doc, op);
    case 'move':
      return execMove(doc, op);
    case 'copy':
      return execCopy(doc, op);
    default: {
      // Exhaustiveness guard; parsePatch already rejected other names.
      const never: never = op.name;
      throw new PatchError('MALFORMED_PATCH', `Unsupported operation ${String(never)}`, op.index);
    }
  }
}

function execTest(doc: JsonValue, op: ResolvedOperation): JsonValue {
  const actual = resolve(doc, op.path);
  if (!jsonEqual(actual, op.value)) {
    throw new PatchError(
      'TEST_FAILURE',
      'Test operation failed: value at path does not equal expected value',
      op.index,
      { path: op.path.raw, expected: op.value, actual },
    );
  }
  return doc;
}

function execRemove(doc: JsonValue, op: ResolvedOperation): JsonValue {
  if (op.path.tokens.length === 0) {
    throw new PatchError(
      'PATH_TYPE_MISMATCH',
      'Cannot remove the document root',
      op.index,
      { path: op.path.raw },
    );
  }
  // RFC 6902 §4.2: the target location MUST exist.
  resolve(doc, op.path);
  const { parent, key } = locateParent(doc, op);
  if (Array.isArray(parent)) {
    const idx = requireExistingIndex(parent, key, op);
    return withParent(doc, op, parent.slice(0, idx).concat(parent.slice(idx + 1)));
  }
  const next: Record<string, JsonValue> = { ...(parent as Record<string, JsonValue>) };
  delete next[key];
  return withParent(doc, op, next);
}

function execAdd(doc: JsonValue, op: ResolvedOperation): JsonValue {
  if (op.path.tokens.length === 0) {
    // Adding at root replaces the whole document (RFC 6902 §4.1).
    return op.value;
  }
  const { parent, key } = locateParent(doc, op);
  if (Array.isArray(parent)) {
    const idx = parseArrayIndex(key);
    if (idx === -1) {
      return withParent(doc, op, parent.concat([op.value]));
    }
    if (idx > parent.length) {
      throw new PatchError(
        'ARRAY_INDEX_OUT_OF_BOUNDS',
        'Cannot add: array index is greater than the number of elements',
        op.index,
        { path: op.path.raw, index: idx, length: parent.length },
      );
    }
    return withParent(doc, op, parent.slice(0, idx).concat([op.value], parent.slice(idx)));
  }
  const next: Record<string, JsonValue> = {
    ...(parent as Record<string, JsonValue>),
    [key]: op.value,
  };
  return withParent(doc, op, next);
}

function execReplace(doc: JsonValue, op: ResolvedOperation): JsonValue {
  if (op.path.tokens.length === 0) {
    return op.value;
  }
  // Target location MUST exist; resolve() proves the full path is present.
  resolve(doc, op.path);
  const { parent, key } = locateParent(doc, op);
  if (Array.isArray(parent)) {
    const idx = requireExistingIndex(parent, key, op);
    const next = parent.slice();
    next[idx] = op.value;
    return withParent(doc, op, next);
  }
  return withParent(doc, op, { ...(parent as Record<string, JsonValue>), [key]: op.value });
}

function execMove(doc: JsonValue, op: ResolvedOperation): JsonValue {
  const from = op.from!;
  // RFC 6902 §4.3: moving to a proper descendant of "from" is impossible.
  if (isProperPrefix(from.tokens, op.path.tokens)) {
    throw new PatchError(
      'MOVE_INTO_DESCENDANT',
      'Cannot move a value into one of its own descendants',
      op.index,
      { from: from.raw, path: op.path.raw },
    );
  }
  const value = resolve(doc, from);
  // Spec-mandated ordering: take the value, remove it, then add it.
  const removed = execRemove(doc, { ...op, path: from });
  return execAdd(removed, { ...op, value, hasValue: true });
}

function execCopy(doc: JsonValue, op: ResolvedOperation): JsonValue {
  const value = resolve(doc, op.from!);
  return execAdd(doc, { ...op, value, hasValue: true });
}

function isProperPrefix(prefixTokens: readonly string[], pathTokens: readonly string[]): boolean {
  if (prefixTokens.length >= pathTokens.length) return false;
  return prefixTokens.every((token, i) => token === pathTokens[i]);
}

function requireExistingIndex(arr: readonly JsonValue[], token: string, op: ResolvedOperation): number {
  const idx = parseArrayIndex(token);
  if (idx === -1 || idx >= arr.length) {
    throw new PatchError(
      'ARRAY_INDEX_OUT_OF_BOUNDS',
      'Array index does not reference an existing element',
      op.index,
      { path: op.path.raw, index: idx === -1 ? arr.length : idx, length: arr.length },
    );
  }
  return idx;
}

interface LocatedParent {
  readonly parent: JsonValue;
  readonly key: string;
}

/**
 * Walk all but the last token, proving the parent chain exists, and return the
 * immediate container plus the final reference token.
 */
function locateParent(doc: JsonValue, op: ResolvedOperation): LocatedParent {
  const tokens = op.path.tokens;
  let parent: JsonValue = doc;
  for (let i = 0; i < tokens.length - 1; i += 1) {
    const token = tokens[i]!;
    if (Array.isArray(parent)) {
      const idx = parseArrayIndex(token);
      if (idx === -1 || idx >= parent.length) {
        throw new PatchError(
          'POINTER_PARENT_MISSING',
          'Parent path passes through a non-existent array element',
          op.index,
          { path: op.path.raw, atToken: token, index: idx === -1 ? parent.length : idx, length: parent.length },
        );
      }
      parent = parent[idx]!;
    } else if (parent !== null && typeof parent === 'object') {
      const obj = parent as Record<string, JsonValue>;
      if (!Object.prototype.hasOwnProperty.call(obj, token)) {
        throw new PatchError(
          'POINTER_PARENT_MISSING',
          'Parent path passes through a non-existent object key',
          op.index,
          { path: op.path.raw, atToken: token },
        );
      }
      parent = obj[token]!;
    } else {
      throw new PatchError(
        'PATH_TYPE_MISMATCH',
        'Parent path traverses a value that is neither an object nor an array',
        op.index,
        { path: op.path.raw, atToken: token, actualType: Array.isArray(parent) ? 'array' : typeof parent },
      );
    }
  }
  // The immediate container must itself be an object or array. Spreading a
  // scalar/number or null would otherwise silently fabricate an object.
  if (parent === null || typeof parent !== 'object') {
    throw new PatchError(
      'PATH_TYPE_MISMATCH',
      'Target parent is neither an object nor an array; it cannot contain a child value',
      op.index,
      {
        path: op.path.raw,
        atToken: tokens[tokens.length - 1]!,
        actualType: parent === null ? 'null' : typeof parent,
      },
    );
  }
  return { parent, key: tokens[tokens.length - 1]! };
}

/**
 * Rebuild the document immutably, replacing the container located by the
 * operation's parent chain with `newParent`.
 */
function withParent(root: JsonValue, op: ResolvedOperation, newParent: JsonValue): JsonValue {
  const tokens = op.path.tokens;
  const rebuild = (node: JsonValue, depth: number): JsonValue => {
    if (depth === tokens.length - 1) return newParent;
    const token = tokens[depth]!;
    if (Array.isArray(node)) {
      const idx = parseArrayIndex(token);
      return node.map((child, i) => (i === idx ? rebuild(child!, depth + 1) : child));
    }
    return {
      ...(node as Record<string, JsonValue>),
      [token]: rebuild((node as Record<string, JsonValue>)[token]!, depth + 1),
    };
  };
  return rebuild(root, 0);
}
