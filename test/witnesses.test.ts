/**
 * Witness tests.
 *
 * For every BREAKING finding the engine must supply a MINIMAL concrete witness
 * value. The independent Python oracle re-validates each witness against the
 * RAW old/new contracts and decides:
 *   request direction  -> accepted by the OLD contract, rejected by the NEW
 *   response direction -> accepted by the NEW contract, rejected by the OLD
 * These tests fail if a witness is missing, is accepted on both sides, or is
 * rejected on the producer side (i.e. a fabricated/incorrect witness).
 */
import { describe, it, expect } from 'vitest';
import { DiffEngine } from '../src/kernel/diff-engine.js';
import { matrix } from './helpers/fixtures.js';
import { askOracle, oracleAvailable } from './helpers/oracle-bridge.js';

const BREAKING_CASES: Array<keyof typeof matrix> = [
  'nullableParamTightened',
  'enumNarrowed',
  'paramMovedLocation',
  'paramBecameRequired',
  'requiredParamAdded',
  'requiredBodyFieldAdded',
  'nullableBodyFieldTightened',
  'bodyFieldEnumNarrowed',
  'responseStatusAdded',
  'responseRequiredFieldRemoved',
  'responseFieldBecameOptional',
  'responseEnumNarrowed',
  'operationRemoved',
];

describe.skipIf(!oracleAvailable())('minimal incompatible witnesses, validated by the oracle', () => {
  const engine = new DiffEngine();

  for (const name of BREAKING_CASES) {
    it(`${name}: witness accepts on producer side, rejects on consumer side`, () => {
      const pair = matrix[name]();
      const { result } = engine.diff(pair.old, pair.new, `witness-${name}`);
      const breaking = result.findings.filter((f) => f.severity === 'BREAKING');
      expect(breaking.length, `${name} must have breaking findings`).toBeGreaterThan(0);

      for (const f of breaking) {
        expect(f.witness, `${name}/${f.code} must carry a witness`).not.toBeNull();
        expect(f.witness?.example).toBeDefined();
        expect(typeof f.witness?.rationale).toBe('string');
      }

      const oracle = askOracle({ old: pair.old, new: pair.new, findings: result.findings });
      const checks = new Map(oracle.witnessChecks.map((c) => [c.id, c]));

      for (const f of breaking) {
        const check = checks.get(f.id);
        expect(check, `oracle must judge ${name}/${f.code} (${f.id})`).toBeDefined();
        expect(check!.ok, `${name}/${f.code} witness invalid: ${check!.reason}`).toBe(true);
      }
    });
  }

  it('nullable tightening witness is literally null and demonstrates both sides', () => {
    const pair = matrix.nullableParamTightened();
    const { result } = engine.diff(pair.old, pair.new);
    const f = result.findings.find((x) => x.code === 'PARAM_TYPE_NARROWED');
    expect(f).toBeDefined();
    // concrete value assertion, not merely presence of a field
    expect((f!.witness!.example as { value: unknown }).value).toBeNull();
    expect(f!.witness!.location).toContain('GET /pets');
  });

  it('enum narrowing witness is the removed literal', () => {
    const pair = matrix.enumNarrowed();
    const { result } = engine.diff(pair.old, pair.new);
    const f = result.findings.find((x) => x.code === 'PARAM_ENUM_NARROWED');
    expect((f!.witness!.example as { value: unknown }).value).toBe('sold');
  });

  it('added-status witness uses the undocumented status code', () => {
    const pair = matrix.responseStatusAdded();
    const { result } = engine.diff(pair.old, pair.new);
    const f = result.findings.find((x) => x.code === 'RESPONSE_STATUS_ADDED');
    expect((f!.witness!.example as { status: number }).status).toBe(429);
  });

  it('compatible contracts produce no breaking witness at all', () => {
    const pair = matrix.identical();
    const { result } = engine.diff(pair.old, pair.new);
    expect(result.compatible).toBe(true);
    expect(result.findings).toHaveLength(0);
  });
});
