/**
 * 引擎门面：编排契约解析 → 静态估计 → 静态预算门 → 运行时执行。
 *
 * 错误归类边界：
 *   parser/validator 产生的 DomainError 原样向上（INPUT_INVALID / STATE_CONFLICT）
 *   静态估计超过预算        → RESOURCE_EXHAUSTED（phase=STATIC）
 *   运行时预算超限          → 正常返回 PARTIAL（不抛错），由 cancelled 标注
 *   适配器/底层计算异常     → COMPUTATION_FAILED
 */
import type {
  CostNode,
  ExecutionResult,
  VariableValues,
} from '../contract/types.js';
import { DomainError } from '../errors/DomainError.js';
import { estimateCost } from '../cost/estimator.js';
import { executePlan } from '../kernel/executor.js';
import { parseQuery } from '../query/parser.js';
import { validateQuery } from '../query/validator.js';
import type { SchemaDocument } from '../contract/types.js';
import type { StateAdapter } from '../state/adapter.js';
import { AdapterError } from '../state/adapter.js';

export interface RunRequest {
  runId: string;
  query: string;
  variables?: VariableValues;
  budget: number;
}

export interface RunOutcome {
  runId: string;
  ok: boolean;
  /** ok=true 时存在 */
  result?: ExecutionResult;
  /** ok=false 时存在（静态阶段失败：输入/冲突/静态预算） */
  error?: {
    category: DomainError['category'];
    phase: 'REQUEST' | 'PARSE' | 'VALIDATE' | 'STATIC_GATE' | 'EXECUTE';
    message: string;
    path?: string;
    context: Record<string, unknown>;
  };
}

export interface RunObserver {
  onPhase(event: PhaseEvent): void;
}

export type PhaseEvent =
  | { runId: string; phase: 'PARSE'; detail: string }
  | { runId: string; phase: 'VALIDATE'; detail: string }
  | { runId: string; phase: 'ESTIMATE'; estimatedCost: number; budget: number; tree: CostNode }
  | { runId: string; phase: 'STATIC_GATE'; allowed: boolean; detail: string }
  | { runId: string; phase: 'EXECUTE'; status: 'COMPLETE' | 'PARTIAL'; actualCost: number; detail: string };

export function runQuery(
  schema: SchemaDocument,
  adapter: StateAdapter,
  req: RunRequest,
  observer?: RunObserver,
): RunOutcome {
  const { runId } = req;
  if (!Number.isInteger(req.budget) || req.budget < 0) {
    return {
      runId,
      ok: false,
      error: {
        category: 'INPUT_INVALID',
        phase: 'REQUEST',
        message: `budget must be a non-negative integer, got ${JSON.stringify(req.budget)}`,
        context: { budget: req.budget },
      },
    };
  }

  type Phase = 'PARSE' | 'VALIDATE' | 'STATIC_GATE' | 'EXECUTE';
  let phase: Phase = 'PARSE';
  try {
    observer?.onPhase({ runId, phase: 'PARSE', detail: 'tokenize + recursive-descent parse' });
    const parsed = parseQuery(req.query, runId);

    phase = 'VALIDATE';
    observer?.onPhase({ runId, phase: 'VALIDATE', detail: 'variable type-check then structural validation' });
    const plan = validateQuery(schema, parsed, req.variables ?? {}, runId);

    const { costTree, total } = estimateCost(plan);
    observer?.onPhase({ runId, phase: 'ESTIMATE', estimatedCost: total, budget: req.budget, tree: costTree });

    phase = 'STATIC_GATE';
    if (total > req.budget) {
      observer?.onPhase({
        runId,
        phase: 'STATIC_GATE',
        allowed: false,
        detail: `estimated ${total} exceeds budget ${req.budget}`,
      });
      throw new DomainError(
        'RESOURCE_EXHAUSTED',
        `static estimate ${total} exceeds budget ${req.budget}`,
        runId,
        { phase: 'STATIC', estimatedCost: total, budget: req.budget },
      );
    }
    observer?.onPhase({
      runId,
      phase: 'STATIC_GATE',
      allowed: true,
      detail: `estimated ${total} within budget ${req.budget}`,
    });

    phase = 'EXECUTE';
    const out = executePlan(plan, adapter, req.budget);
    observer?.onPhase({
      runId,
      phase: 'EXECUTE',
      status: out.status,
      actualCost: out.actualCost,
      detail:
        out.status === 'PARTIAL' && out.cancellation
          ? `cancelled at ${out.cancellation.path}: ${out.cancellation.reason}`
          : 'completed',
    });

    const result: ExecutionResult = {
      runId,
      status: out.status,
      data: out.data,
      costTree: out.costTree,
      estimatedCost: total,
      actualCost: out.actualCost,
      budget: req.budget,
      estimateBelowActual: total < out.actualCost,
      cancelled: out.cancellation,
    };
    return { runId, ok: true, result };
  } catch (err) {
    if (err instanceof DomainError) {
      return {
        runId,
        ok: false,
        error: { category: err.category, phase, message: err.message, path: err.path, context: err.context },
      };
    }
    if (err instanceof AdapterError) {
      return {
        runId,
        ok: false,
        error: {
          category: 'COMPUTATION_FAILED',
          phase,
          message: err.message,
          context: { cause: String(err.causeDetail) },
        },
      };
    }
    return {
      runId,
      ok: false,
      error: {
        category: 'COMPUTATION_FAILED',
        phase,
        message: err instanceof Error ? err.message : String(err),
        context: {},
      },
    };
  }
}
