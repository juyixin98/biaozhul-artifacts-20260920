import { parseContract, type RawContract } from '../contract/parser.js';
import { DomainError } from '../errors.js';
import type { CompositeContract, DataSource } from '../types.js';

/**
 * Contract registry: maps a logical composite name to a parsed, validated
 * contract and the set of data sources its nodes may use. Registration is
 * the only place INVALID_CONTRACT surfaces (never per query).
 */
export class ContractRegistry {
  private readonly contracts = new Map<string, { contract: CompositeContract }>();

  register(raw: RawContract): CompositeContract {
    const contract = parseContract(raw);
    const key = this.contractKey(contract.name, contract.version);
    this.contracts.set(key, { contract });
    // Unversioned pointer resolves to the highest registered version.
    const current = this.contracts.get(contract.name);
    if (!current || contract.version > current.contract.version) {
      this.contracts.set(contract.name, { contract });
    }
    return contract;
  }

  resolve(name: string, version?: number): CompositeContract {
    const key = version ? this.contractKey(name, version) : name;
    const entry = this.contracts.get(key);
    if (!entry) {
      throw new DomainError({
        category: 'NOT_FOUND',
        code: 'CONTRACT_NOT_FOUND',
        message: `no registered composite contract named ${name}${version ? ` v${version}` : ''}`,
        httpStatus: 404,
      });
    }
    return entry.contract;
  }

  list(): Array<{ name: string; version: number; nodes: number; fields: number }> {
    return [...this.contracts.entries()]
      .filter(([key]) => !key.includes(':'))
      .map(([, entry]) => ({
        name: entry.contract.name,
        version: entry.contract.version,
        nodes: entry.contract.nodes.length,
        fields: entry.contract.fields.length,
      }));
  }

  private contractKey(name: string, version: number): string {
    return `${name}:v${version}`;
  }
}

export interface ResolvedSources {
  sources: Map<string, DataSource>;
}
