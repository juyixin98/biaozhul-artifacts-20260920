/**
 * 应用服务：为引擎补充运行编号与诊断记录，是 HTTP 层与 CLI 的共同入口。
 */
import { randomUUID } from 'node:crypto';
import type { SchemaDocument } from '../contract/types.js';
import { runQuery, type RunObserver, type RunOutcome, type RunRequest } from '../engine/engine.js';
import type { StateAdapter } from '../state/adapter.js';
import { RunStore } from './runStore.js';

export interface ServiceHandle {
  run(req: Omit<RunRequest, 'runId'> & { runId?: string }): { runId: string; outcome: RunOutcome };
  store: RunStore;
}

export function createService(schema: SchemaDocument, adapter: StateAdapter, logPath?: string): ServiceHandle {
  const store = new RunStore(logPath);
  const run: ServiceHandle['run'] = (req) => {
    const runId = req.runId ?? `run-${new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 14)}-${randomUUID().slice(0, 8)}`;
    store.start(runId, { query: req.query, variables: req.variables ?? {}, budget: req.budget });
    const observer: RunObserver = {
      onPhase: (event) => store.phase(runId, event),
    };
    const outcome = runQuery(schema, adapter, { ...req, runId }, observer);
    const verdict = buildVerdict(outcome);
    store.finish(
      runId,
      outcome.ok
        ? { kind: 'RESULT', result: outcome.result! }
        : {
            kind: 'ERROR',
            category: outcome.error!.category,
            phase: outcome.error!.phase,
            message: outcome.error!.message,
            path: outcome.error!.path,
            context: outcome.error!.context,
          },
      verdict,
    );
    return { runId, outcome };
  };
  return { run, store };
}

function buildVerdict(outcome: RunOutcome): string {
  if (!outcome.ok) {
    const e = outcome.error!;
    return `REJECTED[${e.category}] at ${e.phase}: ${e.message}`;
  }
  const r = outcome.result!;
  if (r.status === 'COMPLETE') {
    return `COMPLETE estimated=${r.estimatedCost} actual=${r.actualCost} within budget=${r.budget}`;
  }
  const c = r.cancelled!;
  return `PARTIAL estimated=${r.estimatedCost} actual=${r.actualCost} budget=${r.budget}; cancelled at ${c.path} (spent ${c.spent})`;
}
