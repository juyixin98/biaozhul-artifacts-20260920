#!/usr/bin/env node
/**
 * Local CLI: run a contract diff between two YAML/JSON files and print the
 * diagnostics as JSON. No network involved.
 *
 *   npx tsx src/cli.ts tests/fixtures/old.yaml tests/fixtures/new.yaml
 */
import { readFileSync } from 'node:fs';
import { ContractStore } from './state/store.js';
import { DiffService } from './diagnostics/service.js';

const [, , oldPath, newPath] = process.argv;
if (!oldPath || !newPath) {
  process.stderr.write('usage: tsx src/cli.ts <old-contract> <new-contract>\n');
  process.exit(2);
}

const oldContract = readFileSync(oldPath, 'utf8');
const newContract = readFileSync(newPath, 'utf8');

const store = new ContractStore(':memory:');
const out = new DiffService(store).run({
  oldContract,
  newContract,
  oldVersionLabel: oldPath,
  newVersionLabel: newPath,
});

process.stdout.write(`${JSON.stringify(out, null, 2)}\n`);
process.exit(out.status === 'error' ? 1 : 0);
