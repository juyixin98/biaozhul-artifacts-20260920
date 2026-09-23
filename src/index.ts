export { compareLayouts } from './compare.js';
export { parseLayout, parseType, resolveType } from './typeModel.js';
export type {
  ParsedLayout,
  TypeModel,
  TypeKind,
  StructMemberModel,
} from './typeModel.js';
export type {
  SolcStorageLayout,
  SolcStorageLayout as StorageLayout,
  SolcArtifactLike,
  StorageLayoutItem,
  StorageLayoutType,
  CompatReport,
  Finding,
  FindingKind,
  Severity,
  Verdict,
  CheckOptions,
  Evidence,
} from './types.js';

import { compareLayouts } from './compare.js';
import type { SolcArtifactLike, SolcStorageLayout } from './types.js';

/** 宽松入口：接受 storageLayout 本体，或带 storageLayout 字段的 artifact */
export function check(
  oldLayout: SolcStorageLayout | SolcArtifactLike,
  newLayout: SolcStorageLayout | SolcArtifactLike,
  options?: { gapNamePattern?: RegExp },
) {
  const a = unwrap(oldLayout);
  const b = unwrap(newLayout);
  return compareLayouts(a, b, options ?? {});
}

function unwrap(input: SolcStorageLayout | SolcArtifactLike): SolcStorageLayout {
  if (input && !Array.isArray(input) && 'storageLayout' in input && input.storageLayout) {
    return input.storageLayout;
  }
  return input as SolcStorageLayout;
}
