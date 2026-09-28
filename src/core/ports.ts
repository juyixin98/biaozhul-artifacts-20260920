/**
 * State-adapter port. The kernel depends on this interface, never on SQLite,
 * so the transactional execution core can be tested with an in-memory double
 * and swapped to another backend without touching contract logic.
 */

/** One immutable version row, including DELETE tombstones. */
export interface StoredVersion {
  readonly resourceId: string;
  readonly version: number;
  readonly body: unknown;
  readonly createdMs: number;
  readonly deleted: boolean;
}

export interface NewVersion {
  readonly resourceId: string;
  readonly version: number;
  readonly body: unknown;
  readonly createdMs: number;
  readonly deleted: boolean;
}

export interface StoreTransaction {
  /** Latest version row, or null when no row exists or the latest is a tombstone. */
  selectCurrent(resourceId: string): StoredVersion | null;
  /** Highest version number ever appended (including tombstones), or 0. */
  selectMaxVersion(resourceId: string): number;
  /** Append a new immutable version row and return the persisted snapshot. */
  insertVersion(input: NewVersion): StoredVersion;
}

export interface ResourceStore {
  /**
   * Run `fn` in one SERIALIZABLE transaction. The conditional check and the
   * write therefore commit atomically: a concurrent writer cannot sneak a new
   * version between the validator comparison and the insert.
   */
  transact<T>(fn: (tx: StoreTransaction) => T): T;

  // --- Diagnostic / history reads ---------------------------------------
  listVersions(resourceId: string): readonly StoredVersion[];
  selectVersion(resourceId: string, version: number): StoredVersion | null;
  countResources(): number;
  /** Current (non-tombstone) snapshots in id order, with simple limit/offset paging. */
  listCurrent(limit: number, offset: number): readonly StoredVersion[];
}
