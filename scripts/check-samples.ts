import { compareLayouts } from '../src/compare.js';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const manifest = JSON.parse(
  readFileSync(join(here, '..', 'samples', 'manifest.json'), 'utf8'),
) as Record<string, { expect: string }>;

let fail = 0;
for (const [name, meta] of Object.entries(manifest)) {
  const dir = join(here, '..', 'samples', 'pairs', name);
  const o = JSON.parse(readFileSync(join(dir, 'v1.storageLayout.json'), 'utf8'));
  const n = JSON.parse(readFileSync(join(dir, 'v2.storageLayout.json'), 'utf8'));
  const r = compareLayouts(o, n);
  const ok = r.verdict === meta.expect;
  if (!ok) fail++;
  console.log(
    (ok ? 'PASS' : 'FAIL').padEnd(5),
    name.padEnd(34),
    'got=' + r.verdict.padEnd(12),
    'want=' + meta.expect,
    ` E${r.summary.errors} W${r.summary.warnings} I${r.summary.infos}`,
  );
  if (!ok) {
    for (const f of r.findings) console.log('      ', f.severity, f.kind, '@', f.path.join('.'));
  }
}
process.exit(fail ? 1 : 0);
