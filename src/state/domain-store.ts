import type { JsonValue } from '../contract/protocol.js';

/**
 * Synthetic business domain: an append-only key/value ledger used as the
 * local fixture for side effects. It is deliberately separate from the
 * operation/diagnostic tables: an operation record says "RPC X was attempted",
 * while the domain store says "the business state actually changed".
 */

export type KvEntry = {
  key: string;
  value: JsonValue;
  revision: number;
  operationId: string;
  createdAt: number;
};

export interface DomainStore {
  init(): void;
  /** Insert a new key. Throws if the key already exists. */
  put(entry: KvEntry): void;
  get(key: string): KvEntry | null;
  list(): KvEntry[];
  close(): void;
}

export class DuplicateKeyError extends Error {
  constructor(readonly key: string) {
    super(`key already exists: ${key}`);
    this.name = 'DuplicateKeyError';
  }
}
