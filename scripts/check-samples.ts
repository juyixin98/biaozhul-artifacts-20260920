/**
 * Runs every paired sample under samples/ and prints the verdicts.
 * Usage: npm run check:samples
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { compareLayouts } from '../src/index.js';
import { StorageLayout } from '../src/types.js';

const here = dirname(fileURLToPath(import.meta.url));
const samplesDir = join(here, '..', 'samples');

function load(rel: string): StorageLayout {
  return JSON.parse(readFileSync(join(samplesDir, rel), 'utf8')) as StorageLayout;
}

const pairs: Array<{ name: string; oldFile: string; newFile: string; expect: string }> = [
  { name: 'append-fields (compatible)', oldFile: 'v1/Token.storage.json', newFile: 'v2-append/Token.storage.json', expect: 'compatible' },
  { name: 'gap-reduction (compatible)', oldFile: 'v1/Upgradeable.storage.json', newFile: 'v2-gap/Upgradeable.storage.json', expect: 'compatible' },
  { name: 'insert+width+inheritance-move (incompatible)', oldFile: 'v1/Token.storage.json', newFile: 'v2-breaking/Token.storage.json', expect: 'incompatible' },
];

let failed = 0;
for (const p of pairs) {
  const report = compareLayouts(load(p.oldFile), load(p.newFile));
  const ok = report.verdict === p.expect;
  if (!ok) failed++;
  console.log(`\n=== ${p.name}`);
  console.log(`verdict: ${report.verdict} (expected ${p.expect}) ${ok ? 'OK' : 'MISMATCH'}`);
  console.log(`summary: ${report.summary}`);
  for (const f of report.findings) {
    console.log(`  [${f.severity}] ${f.code} @ ${f.path} — ${f.message}`);
  }
}

if (failed > 0) {
  console.error(`\n${failed} sample pair(s) did not match expectations`);
  process.exit(1);
}
console.log('\nall sample pairs match expectations');
