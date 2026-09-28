/**
 * Version identity. Every accepted write appends an immutable version row;
 * the ETag opaque tag combines the monotonically increasing version number
 * with a content hash, so it is unique per committed snapshot and visibly
 * derived from that snapshot's content.
 */
import { contentHash } from './hash.js';

export interface VersionTag {
  readonly opaque: string;
  /** The server emits strong validators: stored JSON is byte-deterministic. */
  readonly weak: false;
}

export function etagFor(version: number, body: unknown): VersionTag {
  return { opaque: `v${version}-${contentHash(body)}`, weak: false };
}

/** A stored resource snapshot. */
export interface ResourceSnapshot {
  readonly id: string;
  readonly version: number;
  readonly body: unknown;
  readonly createdAtMs: number;
}
