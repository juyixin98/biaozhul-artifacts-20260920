/**
 * Matrix tests: each hand-written old/new contract pair is judged by BOTH
 *   - the TypeScript engine (system under test)
 *   - the independent Python reference oracle
 * and the resulting finding multisets must be identical (direction, severity,
 * failure code, operation, path). Expected answers therefore never come from
 * the implementation under test.
 */
import { describe, it, expect } from 'vitest';
import { DiffEngine } from '../src/kernel/diff-engine.js';
import { matrix } from './helpers/fixtures.js';
import { askOracle, oracleAvailable, type ExpectedKey } from './helpers/oracle-bridge.js';
import type { Finding } from '../src/core/types.js';

describe.skipIf(!oracleAvailable())('old/new contract matrix vs independent oracle', () => {
  const engine = new DiffEngine();

  const matrixCases: Array<{
    name: keyof typeof matrix;
    expectCompatible: boolean;
    expectedCodes: string[];
  }> = [
    { name: 'nullableParamTightened', expectCompatible: false, expectedCodes: ['PARAM_TYPE_NARROWED'] },
    { name: 'nullableParamLoosened', expectCompatible: true, expectedCodes: [] },
    { name: 'enumNarrowed', expectCompatible: false, expectedCodes: ['PARAM_ENUM_NARROWED'] },
    { name: 'enumExtended', expectCompatible: true, expectedCodes: ['PARAM_ENUM_EXTENDED'] },
    { name: 'defaultChanged', expectCompatible: true, expectedCodes: ['PARAM_DEFAULT_CHANGED'] },
    { name: 'defaultRemovedOnBodyField', expectCompatible: true, expectedCodes: ['REQUEST_BODY_FIELD_DEFAULT_REMOVED'] },
    { name: 'paramMovedLocation', expectCompatible: false, expectedCodes: ['PARAM_LOCATION_CHANGED'] },
    { name: 'sameNameSecondLocation', expectCompatible: true, expectedCodes: ['OPTIONAL_PARAM_ADDED'] },
    { name: 'paramBecameRequired', expectCompatible: false, expectedCodes: ['PARAM_BECAME_REQUIRED'] },
    { name: 'requiredParamAdded', expectCompatible: false, expectedCodes: ['REQUIRED_PARAM_ADDED'] },
    { name: 'requiredBodyFieldAdded', expectCompatible: false, expectedCodes: ['REQUEST_BODY_REQUIRED_FIELD_ADDED'] },
    { name: 'nullableBodyFieldTightened', expectCompatible: false, expectedCodes: ['REQUEST_BODY_FIELD_TYPE_NARROWED'] },
    { name: 'bodyFieldEnumNarrowed', expectCompatible: false, expectedCodes: ['REQUEST_BODY_FIELD_ENUM_NARROWED'] },
    { name: 'responseStatusAdded', expectCompatible: false, expectedCodes: ['RESPONSE_STATUS_ADDED'] },
    { name: 'responseRequiredFieldRemoved', expectCompatible: false, expectedCodes: ['RESPONSE_REQUIRED_FIELD_REMOVED'] },
    { name: 'responseFieldBecameOptional', expectCompatible: false, expectedCodes: ['RESPONSE_FIELD_BECAME_OPTIONAL'] },
    { name: 'responseEnumNarrowed', expectCompatible: false, expectedCodes: ['RESPONSE_ENUM_NARROWED'] },
    { name: 'operationRemoved', expectCompatible: false, expectedCodes: ['OPERATION_REMOVED'] },
    { name: 'identical', expectCompatible: true, expectedCodes: [] },
  ];

  for (const c of matrixCases) {
    it(`${c.name}: verdict compatible=${c.expectCompatible}, codes [${c.expectedCodes.join(', ')}]`, () => {
      const pair = matrix[c.name]();
      const { result } = engine.diff(pair.old, pair.new, `matrix-${c.name}`);

      // concrete engine assertions, not just "the call worked"
      expect(result.compatible).toBe(c.expectCompatible);
      expect(result.findings.map((f) => f.code).sort()).toEqual([...c.expectedCodes].sort());

      const oracle = askOracle({ old: pair.old, new: pair.new });
      const engineKeys = result.findings
        .map((f): ExpectedKey => [f.direction, f.severity, f.code, f.operation ?? '', f.path])
        .sort(sortKey);
      const oracleKeys = [...oracle.expected].sort(sortKey);

      expect(engineKeys, 'engine finding multiset must match independent oracle').toEqual(oracleKeys);
    });
  }

  it('every breaking matrix case carries at least one BREAKING finding with a code', () => {
    const breakingCases = matrixCases.filter((c) => !c.expectCompatible);
    expect(breakingCases.length).toBeGreaterThanOrEqual(10);
    for (const c of breakingCases) {
      const pair = matrix[c.name]();
      const { result } = engine.diff(pair.old, pair.new);
      const breaking = result.findings.filter((f: Finding) => f.severity === 'BREAKING');
      expect(breaking.length, `${c.name} must carry at least one BREAKING finding`).toBeGreaterThan(0);
      for (const f of breaking) {
        expect(typeof f.code).toBe('string');
        expect(['request', 'response']).toContain(f.direction);
      }
    }
  });
});

function sortKey(a: ExpectedKey, b: ExpectedKey): number {
  return JSON.stringify(a).localeCompare(JSON.stringify(b));
}
