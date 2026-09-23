import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { compareLayouts } from '../src/index.js';
import { StorageLayout } from '../src/types.js';

const samplesDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'samples');
const load = (rel: string): StorageLayout =>
  JSON.parse(readFileSync(join(samplesDir, rel), 'utf8')) as StorageLayout;

describe('paired compiler-output samples', () => {
  it('v1 -> v2-append: appended fields and appended struct member are compatible', () => {
    const r = compareLayouts(load('v1/Token.storage.json'), load('v2-append/Token.storage.json'));
    expect(r.verdict).toBe('compatible');
    const codes = r.findings.map((f) => f.code);
    expect(codes).toContain('FIELD_APPENDED');
    expect(codes).toContain('STRUCT_MEMBER_APPENDED');
    const appended = r.findings.find((f) => f.code === 'STRUCT_MEMBER_APPENDED')!;
    expect(appended.path).toBe('Token.meta.minted');
    const treasury = r.findings.find((f) => f.path === 'Token.treasury')!;
    expect(treasury.evidence.new?.slot).toBe('9');
  });

  it('v1 -> v2-gap: consistent gap reduction is compatible', () => {
    const r = compareLayouts(load('v1/Upgradeable.storage.json'), load('v2-gap/Upgradeable.storage.json'));
    expect(r.verdict).toBe('compatible');
    const codes = r.findings.map((f) => f.code);
    expect(codes).toContain('GAP_REDUCED');
    expect(codes.filter((c) => c === 'FIELD_APPENDED')).toHaveLength(2);
    const gap = r.findings.find((f) => f.code === 'GAP_REDUCED')!;
    expect(gap.path).toBe('Upgradeable.__gap');
    expect(gap.evidence.old?.slot).toBe('2');
    expect(gap.evidence.new?.slot).toBe('3');
  });

  it('v1 -> v2-breaking: insertion, inheritance move and width change are incompatible', () => {
    const r = compareLayouts(load('v1/Token.storage.json'), load('v2-breaking/Token.storage.json'));
    expect(r.verdict).toBe('incompatible');
    const codes = r.findings.map((f) => f.code);
    expect(codes).toContain('FIELD_INSERTED');
    expect(codes).toContain('FIELD_MOVED');
    expect(codes).toContain('TYPE_WIDTH_CHANGED');
    const width = r.findings.find((f) => f.code === 'TYPE_WIDTH_CHANGED')!;
    expect(width.path).toBe('Token.meta.cap');
    expect(width.evidence.old?.numberOfBytes).toBe('16');
    expect(width.evidence.new?.numberOfBytes).toBe('8');
    const inserted = r.findings.find((f) => f.code === 'FIELD_INSERTED')!;
    expect(inserted.path).toBe('Token.initialized');
  });
});
