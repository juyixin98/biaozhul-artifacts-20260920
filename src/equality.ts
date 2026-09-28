/**
 * RFC 8259 value equality used by the "test" operation.
 *
 * - objects compare by their full (unordered) key set and each value
 * - arrays compare element-wise and in order
 * - primitives compare with strict equality, including NaN === NaN
 * - `null`, `true`, `false` are distinct types
 */
export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

export function jsonEqual(a: JsonValue, b: JsonValue): boolean {
  if (a === b) return true;
  if (typeof a === 'number' && typeof b === 'number') {
    // RFC 6902 test uses deep equality; JSON has no NaN, but compare defensibly.
    return Number.isNaN(a) && Number.isNaN(b);
  }
  if (a === null || b === null) return false;
  if (typeof a !== typeof b) return false;
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b)) return false;
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i += 1) {
      if (!jsonEqual(a[i]!, b[i]!)) return false;
    }
    return true;
  }
  if (typeof a === 'object' && typeof b === 'object') {
    const ak = Object.keys(a as Record<string, JsonValue>);
    const bk = Object.keys(b as Record<string, JsonValue>);
    if (ak.length !== bk.length) return false;
    for (const key of ak) {
      if (!Object.prototype.hasOwnProperty.call(b, key)) return false;
      if (!jsonEqual((a as Record<string, JsonValue>)[key]!, (b as Record<string, JsonValue>)[key]!)) {
        return false;
      }
    }
    return true;
  }
  return false;
}
