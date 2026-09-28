import { ErrorCode, type JsonValue } from '../../contract/protocol.js';
import { MethodError } from '../method.js';

/**
 * Strict parameter validators. These deliberately reject everything JS would
 * silently coerce: booleans are not numbers, missing keys are not defaults,
 * integer overflow is an error, not a wrapped float.
 */

export function requireObject(params: JsonValue | undefined, method: string): Record<string, JsonValue> {
  if (params === undefined || typeof params !== 'object' || params === null || Array.isArray(params)) {
    throw new MethodError(
      ErrorCode.INVALID_PARAMS,
      `Invalid params: ${method} expects a JSON object`,
    );
  }
  return params as Record<string, JsonValue>;
}

export function requireArray(params: JsonValue | undefined, method: string): JsonValue[] {
  if (!Array.isArray(params)) {
    throw new MethodError(
      ErrorCode.INVALID_PARAMS,
      `Invalid params: ${method} expects a JSON array`,
    );
  }
  return params;
}

export function stringField(obj: Record<string, JsonValue>, field: string, opts: {
  min?: number;
  max?: number;
  optional?: false;
}): string;
export function stringField(obj: Record<string, JsonValue>, field: string, opts: {
  min?: number;
  max?: number;
  optional: true;
}): string | undefined;
export function stringField(
  obj: Record<string, JsonValue>,
  field: string,
  opts: { min?: number; max?: number; optional?: boolean } = {},
): string | undefined {
  const value = obj[field];
  if (value === undefined) {
    if (opts.optional) return undefined;
    throw new MethodError(ErrorCode.INVALID_PARAMS, `Invalid params: "${field}" is required`);
  }
  if (typeof value !== 'string') {
    throw new MethodError(ErrorCode.INVALID_PARAMS, `Invalid params: "${field}" must be a string`);
  }
  if (opts.min !== undefined && value.length < opts.min) {
    throw new MethodError(
      ErrorCode.INVALID_PARAMS,
      `Invalid params: "${field}" must be at least ${opts.min} characters`,
    );
  }
  if (opts.max !== undefined && value.length > opts.max) {
    throw new MethodError(
      ErrorCode.INVALID_PARAMS,
      `Invalid params: "${field}" must be at most ${opts.max} characters`,
    );
  }
  return value;
}

export function integerField(
  obj: Record<string, JsonValue>,
  field: string,
  bounds: { min?: number; max?: number; optional?: false; default?: number },
): number;
export function integerField(
  obj: Record<string, JsonValue>,
  field: string,
  bounds: { min?: number; max?: number; optional: true; default?: number },
): number | undefined;
export function integerField(
  obj: Record<string, JsonValue>,
  field: string,
  bounds: { min?: number; max?: number; optional?: boolean; default?: number } = {},
): number | undefined {
  const value = obj[field];
  if (value === undefined) {
    if (bounds.default !== undefined) return bounds.default;
    if (bounds.optional) return undefined;
    throw new MethodError(ErrorCode.INVALID_PARAMS, `Invalid params: "${field}" is required`);
  }
  // typeof true === 'boolean' must NOT pass as a number.
  if (typeof value !== 'number' || !Number.isSafeInteger(value)) {
    throw new MethodError(
      ErrorCode.INVALID_PARAMS,
      `Invalid params: "${field}" must be an integer`,
    );
  }
  if (bounds.min !== undefined && value < bounds.min) {
    throw new MethodError(
      ErrorCode.INVALID_PARAMS,
      `Invalid params: "${field}" must be >= ${bounds.min}`,
    );
  }
  if (bounds.max !== undefined && value > bounds.max) {
    throw new MethodError(
      ErrorCode.INVALID_PARAMS,
      `Invalid params: "${field}" must be <= ${bounds.max}`,
    );
  }
  return value;
}

export function booleanField(
  obj: Record<string, JsonValue>,
  field: string,
  optional = false,
): boolean | undefined {
  const value = obj[field];
  if (value === undefined) {
    if (optional) return undefined;
    throw new MethodError(ErrorCode.INVALID_PARAMS, `Invalid params: "${field}" is required`);
  }
  if (typeof value !== 'boolean') {
    throw new MethodError(ErrorCode.INVALID_PARAMS, `Invalid params: "${field}" must be a boolean`);
  }
  return value;
}

export function rejectUnknownFields(obj: Record<string, JsonValue>, allowed: readonly string[]): void {
  for (const key of Object.keys(obj)) {
    if (!allowed.includes(key)) {
      throw new MethodError(
        ErrorCode.INVALID_PARAMS,
        `Invalid params: unknown field "${key}"`,
        { allowed: [...allowed] },
      );
    }
  }
}
