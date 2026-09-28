/**
 * Local CLI for quick, offline verification:
 *   npm run diff -- path/to/old.json path/to/new.json
 * Exits 0 when compatible, 1 when breaking findings exist, 2 on parse error.
 */
import { readFileSync } from 'node:fs';
import { DiffEngine } from '../src/kernel/diff-engine.js';

const [oldPath, newPath] = process.argv.slice(2);
if (!oldPath || !newPath) {
  process.stderr.write('usage: tsx scripts/diff-cli.ts <old.json> <new.json>\n');
  process.exit(2);
}

const readJson = (p: string): unknown =>
  JSON.parse(readFileSync(p, 'utf8')) as unknown;

const engine = new DiffEngine();
const { result, error } = engine.diff(readJson(oldPath), readJson(newPath));

process.stdout.write(`${'=' .repeat(78)}\n`);
process.stdout.write(`requestId : ${result.requestId}\n`);
process.stdout.write(`versions  : ${result.oldVersion} -> ${result.newVersion}\n`);
process.stdout.write(`verdict   : ${result.compatible ? 'COMPATIBLE' : 'INCOMPATIBLE'}\n`);
process.stdout.write(`stats     : ${JSON.stringify(result.stats)}\n`);
process.stdout.write(`${'=' .repeat(78)}\n`);

for (const f of result.findings) {
  process.stdout.write(
    `[${f.severity.padEnd(12)}] ${f.direction.padEnd(8)} ${f.code}\n` +
      `    op     : ${f.operation ?? '(document)'}\n` +
      `    path   : ${f.path || '-'}\n` +
      `    detail : ${f.message}\n`,
  );
  if (f.witness) {
    process.stdout.write(
      `    witness: ${f.witness.location}\n` +
        `             ${JSON.stringify(f.witness.example)}\n` +
        `             ${f.witness.rationale}\n`,
    );
  }
}

if (result.uncertainties.length > 0) {
  process.stdout.write(`\nUncertainties (not judged):\n`);
  for (const u of result.uncertainties) {
    process.stdout.write(`  - ${u.code} @ ${u.location}: ${u.detail}\n`);
  }
}

process.exit(error ? 2 : result.compatible ? 0 : 1);
