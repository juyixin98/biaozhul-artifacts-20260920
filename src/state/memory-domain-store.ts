import type { DomainStore, KvEntry } from './domain-store.js';
import { DuplicateKeyError } from './domain-store.js';

export class MemoryDomainStore implements DomainStore {
  private entries = new Map<string, KvEntry>();

  init(): void {}

  put(entry: KvEntry): void {
    if (this.entries.has(entry.key)) throw new DuplicateKeyError(entry.key);
    this.entries.set(entry.key, { ...entry });
  }

  get(key: string): KvEntry | null {
    const e = this.entries.get(key);
    return e ? { ...e } : null;
  }

  list(): KvEntry[] {
    return [...this.entries.values()].map((e) => ({ ...e }));
  }

  close(): void {
    this.entries.clear();
  }
}
