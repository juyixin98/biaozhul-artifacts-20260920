/**
 * Differential testing: kernel (src/patch.ts) vs the independent reference
 * (test/reference/naiveReference.ts).
 *
 * Neither oracle is produced by the other: the reference is a separately
 * written, in-place-mutating implementation. For every generated document and
 * operation sequence both must agree on:
 *   - success vs failure
 *   - exact resulting document (structural equality)
 *   - failure category and failed-at index
 * plus the deterministic fixtures shipped in /fixtures.
 *
 * Generation is seeded (mulberry32) so failures are reproducible.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { applyPatch } from '../src/patch';
import { referenceApply, type RefJson } from './reference/naiveReference';

const root = process.cwd();
const loadFixture = (rel: string): RefJson =>
  JSON.parse(readFileSync(join(root, rel), 'utf8')) as RefJson;

/* ---------------------------------- rng ---------------------------------- */

function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const makeRng = (seed: number) => {
  const rand = mulberry32(seed);
  const int = (n: number): number => Math.floor(rand() * n);
  const pick = <T,>(xs: readonly T[]): T => {
    if (xs.length === 0) throw new Error('rng.pick called on an empty list');
    return xs[int(xs.length)]!;
  };
  const bool = (p = 0.5): boolean => rand() < p;
  return { rand, int, pick, bool };
};
type Rng = ReturnType<typeof makeRng>;

/* ---------------------------- document shaping --------------------------- */

const KEY_POOL = ['name', 'value', 'items', 'meta', '', 'a/b', 'c~d', 'x~1y', 'k~0j', '0', 'n1'];
const SCALARS = (): RefJson[] => [0, 1, -7, 42, 'alpha', 'beta~', 'a/b', true, false, null];

function randomDoc(rng: Rng, depth: number): RefJson {
  if (depth <= 0 || rng.bool(0.35)) {
    return rng.pick(SCALARS());
  }
  return randomContainer(rng, depth);
}

/** A document node guaranteed to be an object or array. */
function randomContainer(rng: Rng, depth: number): RefJson {
  if (rng.bool(0.45)) {
    const len = rng.int(4);
    const arr: RefJson[] = [];
    for (let i = 0; i < len; i += 1) arr.push(randomDoc(rng, depth - 1));
    return arr;
  }
  const obj: Record<string, RefJson> = {};
  const count = 1 + rng.int(3);
  for (let i = 0; i < count; i += 1) {
    obj[rng.pick(KEY_POOL)] = randomDoc(rng, depth - 1);
  }
  return obj;
}

/** Raw pointer string to each existing location, plus container forms. */
interface Container {
  raw: string;
  kind: 'object' | 'array';
  length: number;
}
interface Locations {
  existing: string[];
  containers: Container[];
}

const escapeToken = (key: string): string => key.replace(/~/g, '~0').replace(/\//g, '~1');

function collectLocations(doc: RefJson): Locations {
  const existing: string[] = [''];
  const containers: Container[] = [];

  const walk = (node: RefJson, prefix: string, depth: number): void => {
    if (depth > 4) return;
    if (Array.isArray(node)) {
      containers.push({ raw: prefix, kind: 'array', length: node.length });
      node.forEach((child, i) => {
        const childPtr = `${prefix}/${i}`;
        existing.push(childPtr);
        walk(child, childPtr, depth + 1);
      });
      return;
    }
    if (node !== null && typeof node === 'object') {
      for (const [key, child] of Object.entries(node)) {
        const childPtr = `${prefix}/${escapeToken(key)}`;
        existing.push(childPtr);
        walk(child, childPtr, depth + 1);
      }
    }
  };
  walk(doc, '', 0);

  // Every object is a possible add/replace/remove container, including root.
  const collectObjects = (node: RefJson, prefix: string): void => {
    if (Array.isArray(node)) {
      node.forEach((child, i) => collectObjects(child, `${prefix}/${i}`));
    } else if (node !== null && typeof node === 'object') {
      containers.push({ raw: prefix, kind: 'object', length: 0 });
      for (const [key, child] of Object.entries(node)) {
        collectObjects(child, `${prefix}/${escapeToken(key)}`);
      }
    }
  };
  collectObjects(doc, '');

  return { existing: [...new Set(existing)], containers };
}

/* ------------------------------ op generation ----------------------------- */

interface RawOp {
  op: string;
  path: string;
  value?: RefJson;
  from?: string;
}

function childToken(rng: Rng, kind: 'object' | 'array', length: number): string {
  if (kind === 'array') {
    const choice = rng.int(5);
    if (choice === 0) return '-';
    if (choice === 1) return String(length); // append by index
    if (choice === 2 && length > 0) return String(rng.int(length)); // insert mid
    if (choice === 3) return String(length + 1 + rng.int(2)); // out of bounds
    return length > 0 ? String(rng.int(length)) : '-';
  }
  return rng.pick([...KEY_POOL, 'fresh', 'zzz']);
}

/** Read the value at an existing pointer in the ORIGINAL generated document. */
function readAt(doc: RefJson, raw: string): RefJson {
  if (raw === '') return structuredClone(doc);
  const tokens = raw
    .split('/')
    .slice(1)
    .map((t) => t.replace(/~1/g, '/').replace(/~0/g, '~'));
  let cur: RefJson = doc;
  for (const token of tokens) {
    cur = Array.isArray(cur) ? cur[Number(token)]! : (cur as Record<string, RefJson>)[token]!;
  }
  return structuredClone(cur);
}

function randomOps(rng: Rng, doc: RefJson, locs: Locations): RawOp[] {
  const count = 1 + rng.int(6);
  const ops: RawOp[] = [];
  for (let i = 0; i < count; i += 1) {
    const op = rng.pick(['test', 'add', 'remove', 'replace', 'move', 'copy'] as const);
    const existingPtr = rng.pick(locs.existing);
    switch (op) {
      case 'test': {
        // 75% assert the value currently present in the ORIGINAL document;
        // if earlier ops changed that location, both implementations simply
        // observe the same failure together.
        const value = rng.bool(0.75) ? readAt(doc, existingPtr) : rng.pick(SCALARS());
        ops.push({ op, path: existingPtr, value });
        break;
      }
      case 'remove':
      case 'replace': {
        // 80% existing location, 20% a fabricated, possibly-invalid leaf.
        const path = rng.bool(0.8) ? existingPtr : fabricatedLeaf(rng, locs);
        ops.push(op === 'remove' ? { op, path } : { op, path, value: rng.pick(SCALARS()) });
        break;
      }
      case 'add': {
        const c = rng.pick(locs.containers);
        const path = c.raw === '' ? `/${childToken(rng, c.kind, c.length)}`
          : `${c.raw}/${childToken(rng, c.kind, c.length)}`;
        ops.push({ op, path, value: rng.bool(0.7) ? rng.pick(SCALARS()) : randomDoc(rng, 2) });
        break;
      }
      case 'move':
      case 'copy': {
        const from = existingPtr;
        if (from !== '' && rng.bool(0.2)) {
          // Deliberately target a proper descendant of "from".
          ops.push({ op, from, path: `${from}/child` });
        } else {
          const c = rng.pick(locs.containers);
          const path = c.raw === '' ? `/${childToken(rng, c.kind, c.length)}`
            : `${c.raw}/${childToken(rng, c.kind, c.length)}`;
          ops.push({ op, from, path });
        }
        break;
      }
    }
  }
  return ops;
}

function fabricatedLeaf(rng: Rng, locs: Locations): string {
  const c = rng.pick(locs.containers);
  const token = rng.pick(['missing', '99', '~x', 'a~2b']);
  return c.raw === '' ? `/${token}` : `${c.raw}/${token}`;
}

/* ------------------------------ comparison -------------------------------- */

function structuralEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a === 'number' && typeof b === 'number') return Number.isNaN(a) && Number.isNaN(b);
  if (a === null || b === null || typeof a !== typeof b) return false;
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((v, i) => structuralEqual(v, b[i]));
  }
  if (typeof a === 'object' && typeof b === 'object') {
    const ka = Object.keys(a as object);
    const kb = Object.keys(b as object);
    if (ka.length !== kb.length) return false;
    return ka.every(
      (k) =>
        Object.prototype.hasOwnProperty.call(b, k) &&
        structuralEqual((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]),
    );
  }
  return false;
}

/* --------------------------------- cases ---------------------------------- */

const FIXTURE_PATCHES: Array<[string, string]> = [
  ['fixtures/documents/library.json', 'fixtures/patches/array-shift.json'],
  ['fixtures/documents/library.json', 'fixtures/patches/escape-and-empty-key.json'],
  ['fixtures/documents/library.json', 'fixtures/patches/mid-failure-test.json'],
  ['fixtures/documents/library.json', 'fixtures/patches/move-into-descendant.json'],
  ['fixtures/documents/library.json', 'fixtures/patches/move-copy-shift.json'],
];

describe('differential: kernel agrees with independent reference', () => {
  it.each(FIXTURE_PATCHES)('fixture pair %s + %s', (docRel, patchRel) => {
    assertAgreement(loadFixture(docRel), loadFixture(patchRel) as unknown as RawOp[]);
  });

  const CASE_COUNT = 1000;
  it(`agrees across ${CASE_COUNT} seeded generated cases`, () => {
    for (let seed = 1; seed <= CASE_COUNT; seed += 1) {
      const rng = makeRng(seed * 2654435761);
      // Force a container root so add/move/copy always have a valid target.
      const doc = randomContainer(rng, 4);
      const ops = randomOps(rng, doc, collectLocations(doc));
      assertAgreement(doc, ops, seed);
    }
  });
});

function assertAgreement(doc: RefJson, ops: RawOp[], seed?: number): void {
  const kernel = applyPatch(doc, ops);
  const ref = referenceApply(doc, ops);
  const tag = seed === undefined ? '' : ` [seed=${seed}, ops=${JSON.stringify(ops)}]`;

  expect(kernel.ok, `ok mismatch${tag}`).toBe(ref.ok);

  if (ref.ok) {
    if (!kernel.ok) throw new Error(`kernel unexpectedly failed${tag}`);
    expect(structuralEqual(kernel.result, ref.result), `result mismatch${tag}`).toBe(true);
    expect(kernel.applied, `applied count mismatch${tag}`).toBe(ref.applied);
  } else {
    if (kernel.ok) throw new Error(`kernel unexpectedly succeeded${tag}`);
    expect(kernel.category, `category mismatch${tag}`).toBe(ref.category);
    expect(kernel.failedAtIndex, `failedAtIndex mismatch${tag}`).toBe(ref.failedAtIndex);
    expect(structuralEqual(kernel.result, doc), `failed result must equal original${tag}`).toBe(true);
  }
}
