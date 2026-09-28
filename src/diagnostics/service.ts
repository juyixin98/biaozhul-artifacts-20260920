import { randomUUID } from 'node:crypto';
import { normalizeDoc } from '../contract/normalize.js';
import { parseContractDocument, ContractParseError } from '../contract/loader.js';
import { diffContracts, type DocumentBundle } from '../kernel/diff.js';
import type { DiffResult, Finding } from '../kernel/types.js';
import type { ContractStore } from '../state/store.js';

/**
 * Orchestration service: parse -> normalize -> kernel diff, with every step
 * appended to the processing log under the request id. Parse failures are
 * returned as a structured error, not a 500.
 */

export interface DiffRequest {
  oldContract: string;
  newContract: string;
  contractRef?: string;
  oldVersionLabel?: string;
  newVersionLabel?: string;
}

export interface ServiceResponse {
  requestId: string;
  analysisId?: string;
  status: 'compatible' | 'breaking' | 'undetermined' | 'error';
  versions: { old: string; new: string };
  result?: DiffResult;
  /** Findings with severity 'breaking', ordered for minimal-witness display. */
  failures?: Finding[];
  /** Findings with severity 'undetermined', listed separately. */
  uncertainties?: Finding[];
  steps: Array<{ step: string; level: string; message: string; at: string }>;
  error?: { code: string; message: string; position?: string };
}

export class DiffService {
  constructor(private readonly store: ContractStore) {}

  run(input: DiffRequest, requestId: string = randomUUID()): ServiceResponse {
    const ref = input.contractRef ?? 'inline';
    const versions = {
      old: input.oldVersionLabel ?? 'old',
      new: input.newVersionLabel ?? 'new',
    };
    const steps: ServiceResponse['steps'] = [];
    const log = (level: 'debug' | 'info' | 'warn' | 'error', step: string, message: string, context: unknown = {}): void => {
      this.store.log(requestId, level, step, message, context);
      steps.push({ step, level, message, at: new Date().toISOString() });
    };

    log('info', 'receive', 'contract diff request received', { ref, versions });

    let oldBundle: DocumentBundle;
    let newBundle: DocumentBundle;
    try {
      log('debug', 'parse-old', 'parsing OLD contract document', { version: versions.old });
      oldBundle = parseSide(input.oldContract);
      log('info', 'parse-old', 'OLD contract normalized', { operations: oldBundle.model.operations.length });

      log('debug', 'parse-new', 'parsing NEW contract document', { version: versions.new });
      newBundle = parseSide(input.newContract);
      log('info', 'parse-new', 'NEW contract normalized', { operations: newBundle.model.operations.length });
    } catch (err) {
      const e = err as Error;
      const position = err instanceof ContractParseError ? err.position : undefined;
      log('error', 'parse', `contract parsing failed: ${e.message}`, { position });
      return {
        requestId,
        status: 'error',
        versions,
        steps,
        error: {
          code: 'CONTRACT_PARSE_ERROR',
          message: e.message,
          position,
        },
      };
    }

    log('info', 'diff', 'running bidirectional compatibility kernel', {
      direction: ['request', 'response'],
    });
    const result = diffContracts(oldBundle, newBundle);
    log('info', 'diff', 'kernel finished', {
      requestFindings: result.requestFindings.length,
      responseFindings: result.responseFindings.length,
      compatible: result.compatible,
    });

    for (const note of result.extensionNotes) {
      log('debug', 'extensions', `x-* extension ${note.change}: ${note.extension} (not judged breaking)`);
    }

    const all = [...result.requestFindings, ...result.responseFindings];
    const failures = all.filter((f) => f.severity === 'breaking');
    const uncertainties = all.filter((f) => f.severity === 'undetermined');
    if (failures.length > 0) log('warn', 'verdict', `breaking findings: ${failures.length}`, { count: failures.length });
    if (uncertainties.length > 0) log('warn', 'verdict', `undetermined findings: ${uncertainties.length}`, { count: uncertainties.length });

    const oldRow = this.store.upsertContract(ref, versions.old, oldBundle.model.title, input.oldContract);
    const newRow = this.store.upsertContract(ref, versions.new, newBundle.model.title, input.newContract);
    const analysis = this.store.saveAnalysis(requestId, oldRow.id, newRow.id, result);
    log('info', 'persist', 'analysis persisted to SQLite', { analysisId: analysis.id });

    const status = failures.length > 0 ? 'breaking' : uncertainties.length > 0 ? 'undetermined' : 'compatible';
    return {
      requestId,
      analysisId: analysis.id,
      status,
      versions,
      result,
      failures,
      uncertainties,
      steps,
    };
  }
}

function parseSide(text: string): DocumentBundle {
  const raw = parseContractDocument(text);
  return { model: normalizeDoc(raw), raw };
}
