/**
 * RFC 6901 JSON Pointer resolution.
 *
 * A pointer is a string of `/`-separated reference tokens. Each token is
 * decoded by replacing `~1` with `/` and `~0` with `~` (order matters).
 * The empty string points at the whole document.
 *
 * Array index tokens must match `^(0|[1-9][0-9]*)$`; exactly `-` refers to
 * the one-past-the-end insert position and can never be read.
 */

export type JsonContainer = { [key: string]: unknown } | unknown[];

export class PointerError extends TypeError {
  constructor(
    message: string,
    readonly pointer: string,
    readonly segmentIndex = -1,
  ) {
    super(message);
    this.name = 'PointerError';
  }
}

/** Split a pointer into its decoded reference tokens. */
export function parsePointer(pointer: string): string[] {
  if (pointer === '') return [];
  if (!pointer.startsWith('/')) {
    throw new PointerError(
      `non-empty JSON Pointer must start with '/': ${JSON.stringify(pointer)}`,
      pointer,
    );
  }
  return pointer
    .substring(1)
    .split('/')
    .map((token) => token.replace(/~1/g, '/').replace(/~0/g, '~'));
}

/** Encode segments back into an RFC 6901 pointer string. */
export function formatPointer(segments: readonly string[]): string {
  return segments
    .map((segment) => '/' + String(segment).replace(/~/g, '~0').replace(/\//g, '~1'))
    .join('');
}

/**
 * Return the parent container and the final token for `pointer`.
 *
 * Traversal follows the *current* document, so moved/replaced parents are
 * handled correctly by callers that resolve lazily.
 */
export function locateParent(
  doc: unknown,
  pointer: string,
  segments?: string[],
): { parent: JsonContainer; key: string } {
  const tokens = segments ?? parsePointer(pointer);
  if (tokens.length === 0) {
    throw new PointerError(
      `the empty pointer ('') addresses the whole document and has no parent container`,
      pointer,
    );
  }
  let current: unknown = doc;
  for (let i = 0; i < tokens.length - 1; i++) {
    const token = tokens[i]!;
    current = step(current, token, pointer, i);
  }
  const key = tokens[tokens.length - 1]!;
  if (current === null || typeof current !== 'object') {
    throw new PointerError(
      `cannot index into non-container at segment '${key}'`,
      pointer,
      tokens.length - 1,
    );
  }
  return { parent: current as JsonContainer, key };
}

/** Resolve and return the value at `pointer` (reading `-` is an error). */
export function resolvePointer(doc: unknown, pointer: string): unknown {
  const tokens = parsePointer(pointer);
  if (tokens.length === 0) return doc;
  const { parent, key } = locateParent(doc, pointer, tokens);
  if (Array.isArray(parent)) {
    const index = arrayIndex(key, parent.length, pointer, tokens.length - 1, {
      allowDash: false,
      allowEnd: false,
    });
    if (!Object.prototype.hasOwnProperty.call(parent, index)) {
      throw new PointerError(
        `array index ${index} has no value (sparse array)`,
        pointer,
        tokens.length - 1,
      );
    }
    return parent[index];
  }
  if (!Object.prototype.hasOwnProperty.call(parent, key)) {
    throw new PointerError(
      `object has no member ${JSON.stringify(key)}`,
      pointer,
      tokens.length - 1,
    );
  }
  return (parent as Record<string, unknown>)[key];
}

function step(current: unknown, token: string, pointer: string, depth: number): unknown {
  if (Array.isArray(current)) {
    const index = arrayIndex(token, current.length, pointer, depth, {
      allowDash: false,
      allowEnd: false,
    });
    if (!Object.prototype.hasOwnProperty.call(current, index)) {
      throw new PointerError(`array index ${index} has no value`, pointer, depth);
    }
    return current[index];
  }
  if (current !== null && typeof current === 'object') {
    if (!Object.prototype.hasOwnProperty.call(current, token)) {
      throw new PointerError(
        `object has no member ${JSON.stringify(token)}`,
        pointer,
        depth,
      );
    }
    return (current as Record<string, unknown>)[token];
  }
  throw new PointerError(
    `cannot traverse token ${JSON.stringify(token)} into non-container`,
    pointer,
    depth,
  );
}

interface IndexOptions {
  /** Allow the `-` token (one-past-the-end insert marker). */
  allowDash: boolean;
  /** Allow index === length (append boundary, used by `add`). */
  allowEnd: boolean;
}

/** Validate a numeric array-index token against the current length. */
export function arrayIndex(
  token: string,
  length: number,
  pointer: string,
  segmentIndex: number,
  options: IndexOptions,
): number {
  if (token === '-') {
    if (!options.allowDash) {
      throw new PointerError(
        `'-' (after-end marker) cannot be read or removed`,
        pointer,
        segmentIndex,
      );
    }
    return length;
  }
  if (!/^(0|[1-9][0-9]*)$/.test(token)) {
    throw new PointerError(
      `invalid array index token ${JSON.stringify(token)}`,
      pointer,
      segmentIndex,
    );
  }
  const index = Number(token);
  if (Number.isSafeInteger(index) === false) {
    throw new PointerError(`array index ${token} exceeds safe integer range`, pointer, segmentIndex);
  }
  const bound = options.allowEnd ? length : length - 1;
  if (index > bound) {
    throw new PointerError(
      `array index ${index} is out of bounds (length ${length})`,
      pointer,
      segmentIndex,
    );
  }
  return index;
}

/** RFC 6902 prefix test: `from` points at `to` itself or at one of its ancestors. */
export function isEqualOrPrefix(from: string, to: string): boolean {
  return from === to || to.startsWith(from + '/');
}
