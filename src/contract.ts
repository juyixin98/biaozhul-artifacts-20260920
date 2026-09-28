/**
 * Contract parsing & validation for constrained RFC 6902 patches.
 *
 * This module performs *structural* validation only: op names, member types,
 * pointer syntax, path/from relationships that do not need document state.
 * Semantic errors that require the document (missing members, bad indices,
 * test failures) are produced by the execution kernel.
 */

import { isEqualOrPrefix, parsePointer, PointerError } from './json-pointer.js';

export type PatchOp = 'test' | 'add' | 'remove' | 'replace' | 'move' | 'copy';

export interface PatchOperation {
  op: PatchOp;
  path: string;
  /** add/test/replace: literal JSON value. move/copy: absent. */
  value?: unknown;
  /** move/copy only. */
  from?: string;
}

export interface ParsedPatch {
  operations: PatchOperation[];
}

export type ContractFailureCategory =
  | 'MALFORMED_JSON'
  | 'PATCH_NOT_ARRAY'
  | 'OP_NOT_OBJECT'
  | 'UNKNOWN_OP'
  | 'MISSING_FIELD'
  | 'BAD_FIELD_TYPE'
  | 'MALFORMED_POINTER'
  | 'MOVE_INTO_SELF'
  | 'EMPTY_PATCH';

export class ContractError extends Error {
  constructor(
    readonly category: ContractFailureCategory,
    message: string,
    /** Operation index inside the patch array, -1 when not operation-scoped. */
    readonly operationIndex = -1,
    readonly field?: string,
  ) {
    super(message);
    this.name = 'ContractError';
  }
}

const KNOWN_OPS: ReadonlySet<string> = new Set([
  'add',
  'remove',
  'replace',
  'move',
  'copy',
  'test',
]);

function isPlainJsonObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Parse a raw JSON string into a validated, normalized patch operation list. */
export function parsePatchJson(raw: string): ParsedPatch {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (cause) {
    throw new ContractError(
      'MALFORMED_JSON',
      `patch body is not valid JSON: ${(cause as Error).message}`,
    );
  }
  return parsePatch(parsed);
}

/** Validate an already-parsed JSON value as a patch. */
export function parsePatch(input: unknown): ParsedPatch {
  if (!Array.isArray(input)) {
    throw new ContractError(
      'PATCH_NOT_ARRAY',
      'a JSON Patch document MUST be an array of operation objects',
    );
  }
  if (input.length === 0) {
    throw new ContractError(
      'EMPTY_PATCH',
      'the patch MUST contain at least one operation (constrained service)',
    );
  }

  const operations: PatchOperation[] = input.map((rawOp, index) =>
    normalizeOperation(rawOp, index),
  );

  // Relationship checks that need no document state.
  for (const [index, op] of operations.entries()) {
    if (op.op === 'move' && isEqualOrPrefix(op.from!, op.path)) {
      throw new ContractError(
        'MOVE_INTO_SELF',
        `move 'from' (${op.from}) is equal to, or an ancestor of, 'path' (${op.path}); ` +
          'a value cannot be moved into itself or its own descendant',
        index,
      );
    }
  }

  return { operations };
}

function normalizeOperation(rawOp: unknown, index: number): PatchOperation {
  if (!isPlainJsonObject(rawOp)) {
    throw new ContractError(
      'OP_NOT_OBJECT',
      `operation at index ${index} MUST be a JSON object`,
      index,
    );
  }

  // Reject duplicate op/path/from/value keys early by checking the raw
  // JSON text? JSON.parse keeps the last duplicate; RFC 7493 / common
  // practice accepts the parsed value, which is what we do.
  const { op, path, value, from, ...rest } = rawOp;
  const unknownFields = Object.keys(rest);

  if (typeof op !== 'string') {
    throw new ContractError(
      'MISSING_FIELD',
      `operation at index ${index} is missing string member 'op'`,
      index,
      'op',
    );
  }
  if (!KNOWN_OPS.has(op)) {
    throw new ContractError(
      'UNKNOWN_OP',
      `operation at index ${index} has unsupported op ${JSON.stringify(op)}; ` +
        `allowed: add, remove, replace, move, copy, test`,
      index,
      'op',
    );
  }

  if (typeof path !== 'string') {
    throw new ContractError(
      'MISSING_FIELD',
      `operation at index ${index} ('${op}') is missing string member 'path'`,
      index,
      'path',
    );
  }
  assertPointerSyntax(path, index, 'path');

  const operation: PatchOperation = { op: op as PatchOp, path };

  if (unknownFields.length > 0) {
    // RFC 6902 says unknown members SHOULD be ignored; this constrained
    // service rejects them so contracts stay explicit. Listed as a typed
    // failure rather than silently accepted.
    throw new ContractError(
      'BAD_FIELD_TYPE',
      `operation at index ${index} ('${op}') has unknown member(s): ${unknownFields.join(', ')}`,
      index,
      unknownFields[0],
    );
  }

  switch (op) {
    case 'add':
    case 'replace':
    case 'test':
      if (!('value' in rawOp)) {
        throw new ContractError(
          'MISSING_FIELD',
          `operation at index ${index} ('${op}') MUST contain member 'value'`,
          index,
          'value',
        );
      }
      operation.value = value;
      break;
    case 'remove':
      if ('value' in rawOp) {
        throw fieldNotAllowed(index, op, 'value');
      }
      if ('from' in rawOp) {
        throw fieldNotAllowed(index, op, 'from');
      }
      break;
    case 'move':
    case 'copy':
      if (typeof from !== 'string') {
        throw new ContractError(
          'MISSING_FIELD',
          `operation at index ${index} ('${op}') MUST contain string member 'from'`,
          index,
          'from',
        );
      }
      assertPointerSyntax(from, index, 'from');
      operation.from = from;
      if ('value' in rawOp) {
        throw fieldNotAllowed(index, op, 'value');
      }
      break;
  }

  return operation;
}

function fieldNotAllowed(index: number, op: string, field: string): ContractError {
  return new ContractError(
    'BAD_FIELD_TYPE',
    `operation at index ${index} ('${op}') MUST NOT contain member '${field}'`,
    index,
    field,
  );
}

function assertPointerSyntax(pointer: string, index: number, field: 'path' | 'from'): void {
  try {
    parsePointer(pointer);
  } catch (cause) {
    if (cause instanceof PointerError) {
      throw new ContractError(
        'MALFORMED_POINTER',
        `operation at index ${index} has malformed '${field}': ${cause.message}`,
        index,
        field,
      );
    }
    throw cause;
  }
}
