/**
 * JSON Merge Patch (RFC 7386): patch objects merge recursively; any member
 * whose value is null is deleted from the target. Arrays and scalars in the
 * patch replace wholesale. The input target is never mutated.
 */
export function applyMergePatch(target: unknown, patch: unknown): unknown {
  if (patch === null || typeof patch !== 'object' || Array.isArray(patch)) {
    return structuredClone(patch);
  }
  if (target === null || typeof target !== 'object' || Array.isArray(target)) {
    // Merging an object patch into a non-object target starts from an empty object.
    target = {};
  }
  const result: Record<string, unknown> = structuredClone(target as Record<string, unknown>);
  for (const [key, value] of Object.entries(patch as Record<string, unknown>)) {
    if (value === null) {
      delete result[key];
    } else if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      result[key] = applyMergePatch(result[key], value);
    } else {
      result[key] = structuredClone(value);
    }
  }
  return result;
}
