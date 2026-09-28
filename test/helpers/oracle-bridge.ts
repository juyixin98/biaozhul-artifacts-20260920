/**
 * Bridge to the independent Python reference oracle (test/oracle/contract_oracle.py).
 *
 * The oracle is a separate runtime with a separately-written implementation.
 * Tests ask it two questions:
 *   1. what finding tuples SHOULD exist for a given old/new document pair;
 *   2. does each BREAKING witness genuinely accept on the producer side and
 *      reject on the consumer side of the RAW contracts.
 */
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const ORACLE_PATH = fileURLToPath(new URL('../oracle/contract_oracle.py', import.meta.url));

export type ExpectedKey = [
  direction: string,
  severity: string,
  code: string,
  operation: string,
  path: string,
];

export interface WitnessCheck {
  id: string;
  ok: boolean;
  reason: string;
}

export interface OracleAnswer {
  expected: ExpectedKey[];
  witnessChecks: WitnessCheck[];
  uncertainties: Array<{ code: string; location: string; side?: string }>;
}

export interface OraclePayload {
  old: unknown;
  new: unknown;
  findings?: unknown[];
}

export function askOracle(payload: OraclePayload): OracleAnswer {
  const proc = spawnSync('python3', [ORACLE_PATH], {
    input: JSON.stringify(payload),
    encoding: 'utf8',
    maxBuffer: 16 * 1024 * 1024,
  });
  if (proc.status !== 0) {
    throw new Error(
      `reference oracle failed (status ${proc.status}): ${proc.stderr || proc.stdout}`,
    );
  }
  return JSON.parse(proc.stdout) as OracleAnswer;
}

export function oracleAvailable(): boolean {
  const proc = spawnSync('python3', ['--version'], { encoding: 'utf8' });
  return proc.status === 0;
}
