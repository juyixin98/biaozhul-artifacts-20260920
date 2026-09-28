/**
 * 执行内核。
 *
 * 管线（每一步都写入诊断日志）：
 *   解析形状 → 变量类型校验 → 片段展开 → 选择集/类型校验 → 变量参数校验
 *   → 静态成本估计 → 预算网关（拒绝则永不执行）
 *   → 运行：加载根 → 逐解析器扣减 → 超限取消并保留部分结果
 *
 * 运行时扣减粒度是“单个解析器”：每个标量字段按倍率扣一次；列表先
 * 解析出真实基数，再逐元素、逐叶子扣减。因此“短而深”的列表会在深层
 * 被扣停，已完成的前缀元素保留，未启动的解析器标记 aborted。
 */
import type { FieldNode } from '../contracts/ast.js';
import type { Schema } from '../contracts/schema.js';
import type { CostNode } from '../contracts/cost.js';
import type { RunResult, BudgetWarning } from '../contracts/result.js';
import { QueryError, fail } from '../contracts/errors.js';
import { parseQuery } from '../resolution/parse.js';
import { validateVars, validateVarArgs } from '../resolution/variables.js';
import { expandFragments, expandTopLevelIncludes } from '../resolution/fragments.js';
import { validateSelection } from '../resolution/validate.js';
import { estimateStatic } from './estimator.js';
import { BudgetAbortError, RunContext } from './context.js';
import type { EntityRecord, EntityStore, ListFilter } from '../state/store.js';
import type { RunLogger, RunLogEntry } from '../diagnostics/run-log.js';

export interface EngineDeps {
  schema: Schema;
  store: EntityStore;
  logger: RunLogger;
}

export interface ExplainResult {
  runId: string;
  estimatedTotal: number;
  budget: number;
  accepted: boolean;
  reason: string;
  costTree: CostNode | null;
  error?: import('../contracts/errors.js').Diagnostic;
}

/** 运行中每个成本节点的实际发生额聚合（节点可能随列表元素重复出现）。 */
interface ActualTotals {
  leafCost: number;
  cardinality: number;
}

export class QueryEngine {
  constructor(private readonly deps: EngineDeps) {}

  async execute(rawQuery: unknown, budget: number): Promise<RunResult> {
    const { schema, store, logger } = this.deps;
    const log = logger.newRun(budget, rawQuery);
    const base: RunResult = {
      runId: log.runId,
      status: 'failed',
      budget,
      estimatedTotal: 0,
      consumed: 0,
      data: null,
      costTree: null,
      warnings: [],
      ledger: [],
    };

    if (typeof budget !== 'number' || !Number.isFinite(budget) || budget < 0) {
      const diag = {
        category: 'INPUT_ERROR' as const,
        code: 'MALFORMED_QUERY' as const,
        message: 'budget must be a non-negative finite number',
        details: { budget },
      };
      log.decisionReason = diag.message;
      log.error = diag;
      log.finalStatus = 'failed';
      logger.commit(log);
      return { ...base, error: diag };
    }

    try {
      const resolved = this.resolveStatic(rawQuery, log);
      const { query, vars, expanded, estimate } = resolved;
      base.estimatedTotal = estimate.total;
      base.costTree = estimate.tree;

      // 预算网关：估计超限直接拒绝，不加载任何状态、不启动任何解析器。
      if (estimate.total > budget) {
        return this.staticRejection(log, base, estimate.total, budget);
      }
      log.estimatedTotal = estimate.total;
      log.decisionReason = `static estimate ${estimate.total} <= budget ${budget}; accepted`;
      logger.phase(log, 'gateway-accepted', { estimatedTotal: estimate.total, budget });

      logger.phase(log, 'execute');
      const ctx = new RunContext(vars, budget);
      const warnings: BudgetWarning[] = [];
      const actuals = new WeakMap<CostNode, ActualTotals>();
      const bump = (cn: CostNode, patch: Partial<ActualTotals>): void => {
        const cur = actuals.get(cn) ?? { leafCost: 0, cardinality: 0 };
        actuals.set(cn, {
          leafCost: cur.leafCost + (patch.leafCost ?? 0),
          cardinality: cur.cardinality + (patch.cardinality ?? 0),
        });
      };

      const root = await this.loadRoot(store, query.root, query.rootId, log);
      const data: Record<string, unknown> = {};

      try {
        await this.executeFields(
          expanded,
          estimate.tree.children,
          root,
          query.root,
          vars,
          ctx,
          [],
          data,
          actuals,
          bump,
          log,
          warnings,
        );
      } catch (err) {
        if (err instanceof BudgetAbortError) {
          return this.partialResult(log, base, estimate.tree, actuals, ctx, data, warnings, budget);
        }
        throw err;
      }

      finalizeActuals(estimate.tree, actuals);
      for (const cn of walk(estimate.tree)) if (cn.runtimeStatus === 'pending') cn.runtimeStatus = 'done';
      log.consumed = ctx.consumed;
      log.ledger = ctx.ledger;
      log.finalStatus = 'ok';
      logger.commit(log);
      return {
        ...base,
        status: 'ok',
        consumed: ctx.consumed,
        data,
        warnings,
        ledger: [...ctx.ledger],
      };
    } catch (err) {
      const diagnostic = toDiagnostic(err);
      log.finalStatus = 'failed';
      log.error = diagnostic;
      log.decisionReason = `${diagnostic.category}/${diagnostic.code}: ${diagnostic.message}`;
      logger.commit(log);
      return { ...base, error: diagnostic };
    }
  }

  /** 静态网关拒绝的统一构造（含日志落盘）。 */
  private staticRejection(
    log: RunLogEntry,
    base: RunResult,
    estimatedTotal: number,
    budget: number,
  ): RunResult {
    const reason = `static estimate ${estimatedTotal} exceeds budget ${budget}; execution never started`;
    const diag = {
      category: 'RESOURCE_EXHAUSTED' as const,
      code: 'BUDGET_EXCEEDED_STATIC' as const,
      message: reason,
      details: { estimatedTotal, budget },
    };
    log.estimatedTotal = estimatedTotal;
    log.decisionReason = reason;
    log.finalStatus = 'rejected_static';
    log.error = diag;
    this.deps.logger.commit(log);
    return { ...base, status: 'rejected_static', error: diag };
  }

  /** 运行中取消：标注未完成解析器、回填实际成本、构造部分结果并落盘。 */
  private partialResult(
    log: RunLogEntry,
    base: RunResult,
    tree: CostNode,
    actuals: WeakMap<CostNode, ActualTotals>,
    ctx: RunContext,
    data: Record<string, unknown>,
    warnings: BudgetWarning[],
    budget: number,
  ): RunResult {
    sweepAborted(tree);
    finalizeActuals(tree, actuals);
    this.deps.logger.phase(log, 'aborted', {
      consumed: ctx.consumed,
      ledgerSteps: ctx.ledger.length,
    });
    log.consumed = ctx.consumed;
    log.ledger = ctx.ledger;
    log.finalStatus = 'partial';
    const at = ctx.ledger.findLast((s) => s.rejected)?.at ?? '(unknown)';
    log.decisionReason = `runtime budget exhausted at "${at}": consumed ${ctx.consumed}/${budget}`;
    const abort: import('../contracts/result.js').AbortInfo = {
      code: 'BUDGET_EXCEEDED_RUNTIME',
      category: 'RESOURCE_EXHAUSTED',
      message: log.decisionReason,
      at: at.split(' / '),
      budget,
      consumed: ctx.consumed,
    };
    log.error = abort;
    this.deps.logger.commit(log);
    return {
      ...base,
      status: 'partial',
      consumed: ctx.consumed,
      data,
      warnings,
      ledger: [...ctx.ledger],
      abort,
    };
  }

  /**
   * 静态解析管线：形状解析 → 变量类型校验 → 片段展开 → 选择集校验
   * → 变量参数校验 → 静态成本估计。执行与 explain 共用，保证网关判定
   * 与实际执行前看到的成本树严格一致。
   */
  private resolveStatic(
    rawQuery: unknown,
    log: RunLogEntry,
  ): {
    query: import('../contracts/ast.js').Query;
    vars: ReadonlyMap<string, import('../contracts/ast.js').VarValue>;
    expanded: FieldNode[];
    estimate: import('./estimator.js').StaticEstimate;
  } {
    const { schema, logger } = this.deps;
    logger.phase(log, 'parse');
    const query = parseQuery(rawQuery);

    logger.phase(log, 'validate-vars');
    const vars = validateVars(query);

    logger.phase(log, 'expand-fragments');
    const topExpanded = query.fragments
      ? expandTopLevelIncludes(query.fragments, schema)
      : [];
    const expanded = [
      ...topExpanded,
      ...expandFragments(query.fields.map((f) => ({ ...f })), schema),
    ];

    logger.phase(log, 'validate-selection');
    if (!schema.types[query.root]) {
      fail('INPUT_ERROR', 'UNKNOWN_TYPE', `unknown root type: ${query.root}`);
    }
    validateSelection(expanded, schema, query.root, []);
    validateVarArgs(expanded, schema, query.root, vars, []);

    logger.phase(log, 'estimate-static');
    const estimate = estimateStatic(query.root, expanded, schema);
    return { query, vars, expanded, estimate };
  }

  /** 只解释、不执行：返回网关判定与逐字段静态成本树（不接触状态层）。 */
  explain(rawQuery: unknown, budget: number): ExplainResult {
    const { logger } = this.deps;
    const log = logger.newRun(budget, rawQuery);
    const base: ExplainResult = {
      runId: log.runId,
      estimatedTotal: 0,
      budget,
      accepted: false,
      reason: '',
      costTree: null,
    };
    try {
      if (typeof budget !== 'number' || !Number.isFinite(budget) || budget < 0) {
        throw new QueryError({
          category: 'INPUT_ERROR',
          code: 'MALFORMED_QUERY',
          message: 'budget must be a non-negative finite number',
          details: { budget },
        });
      }
      const { estimate } = this.resolveStatic(rawQuery, log);
      const accepted = estimate.total <= budget;
      const reason = accepted
        ? `static estimate ${estimate.total} <= budget ${budget}; gateway would accept`
        : `static estimate ${estimate.total} > budget ${budget}; gateway would reject`;
      log.estimatedTotal = estimate.total;
      log.decisionReason = reason;
      log.finalStatus = accepted ? 'ok' : 'rejected_static';
      logger.commit(log);
      return {
        ...base,
        estimatedTotal: estimate.total,
        accepted,
        reason,
        costTree: estimate.tree,
      };
    } catch (err) {
      const diagnostic = toDiagnostic(err);
      log.finalStatus = 'failed';
      log.error = diagnostic;
      log.decisionReason = `${diagnostic.category}/${diagnostic.code}`;
      logger.commit(log);
      return { ...base, reason: log.decisionReason, error: diagnostic };
    }
  }

  private async loadRoot(
    store: EntityStore,
    root: string,
    rootId: number,
    log: RunLogEntry,
  ): Promise<EntityRecord> {
    try {
      const rec = await store.load(root, rootId);
      if (!rec) {
        fail(
          'STATE_CONFLICT',
          'ROOT_NOT_FOUND',
          `root ${root}#${rootId} does not exist`,
          [root],
          { rootId },
        );
      }
      return rec;
    } catch (err) {
      if (err instanceof QueryError) throw err;
      this.deps.logger.phase(log, 'store-failure', { cause: messageOf(err) });
      RunContext.toStoreFailure(err, [root]);
    }
  }

  private async executeFields(
    fields: FieldNode[],
    costNodes: CostNode[],
    row: EntityRecord,
    typeName: string,
    vars: ReadonlyMap<string, import('../contracts/ast.js').VarValue>,
    ctx: RunContext,
    path: string[],
    out: Record<string, unknown>,
    actuals: WeakMap<CostNode, ActualTotals>,
    bump: (cn: CostNode, patch: Partial<ActualTotals>) => void,
    log: RunLogEntry,
    warnings: BudgetWarning[],
  ): Promise<void> {
    const { schema } = this.deps;
    for (let i = 0; i < fields.length; i += 1) {
      const node = fields[i]!;
      const cn = costNodes[i]!;
      // 展开后的选择集不应残留 spread；残留属于内部不变量被破坏。
      if (node.kind === 'spread') {
        fail('INPUT_ERROR', 'MALFORMED_QUERY', 'spread reached executor without expansion', path);
      }
      const here = [...path, node.alias];

      try {
        // 取消后到达的解析器一律不启动。
        ctx.checkAborted(here.join(' / '));

        if (node.kind === 'field') {
          const fieldDef = schema.types[typeName]!.fields[node.field]!;
          ctx.charge(fieldDef.multiplier, here.join(' / '));
          out[node.alias] = decodeScalar(row[node.field], fieldDef.type);
          cn.runtimeStatus = 'done';
          bump(cn, { leafCost: fieldDef.multiplier });
          continue;
        }

        await this.executeList(node, cn, row, typeName, vars, ctx, here, out, actuals, bump, log, warnings);
        cn.runtimeStatus = 'done';
      } catch (err) {
        if (err instanceof BudgetAbortError) {
          // 本解析器（或其子代）中断：标记并向上传播，让未完成状态沿
          // 祖先链显式可见；部分结果保留在 out 中已写入的键。
          cn.runtimeStatus = 'aborted';
          sweepAborted(cn);
          throw err;
        }
        throw err;
      }
    }
  }

  /** 解析单个列表：取真实基数 → 上界告警 → 逐元素执行子选择。 */
  private async executeList(
    node: Extract<FieldNode, { kind: 'list' }>,
    cn: CostNode,
    row: EntityRecord,
    typeName: string,
    vars: ReadonlyMap<string, import('../contracts/ast.js').VarValue>,
    ctx: RunContext,
    here: string[],
    out: Record<string, unknown>,
    actuals: WeakMap<CostNode, ActualTotals>,
    bump: (cn: CostNode, patch: Partial<ActualTotals>) => void,
    log: RunLogEntry,
    warnings: BudgetWarning[],
  ): Promise<void> {
    const { schema, store, logger } = this.deps;
    const rel = schema.types[typeName]!.relations[node.relation]!;
    const filters: ListFilter[] = (node.args ?? []).map((a) => ({
      column: a.var,
      value: vars.get(a.var)!,
    }));
    let rows: EntityRecord[];
    try {
      rows = await store.resolveList(node.relation, typeName, row.id, rel.to, filters);
    } catch (err) {
      logger.phase(log, 'store-failure', { at: here, cause: messageOf(err) });
      RunContext.toStoreFailure(err, here);
    }
    cn.actualCardinality = rows.length;
    bump(cn, { cardinality: rows.length });
    if (rows.length > node.declaredUpperBound) {
      warnings.push({
        code: 'DECLARED_BOUND_EXCEEDED',
        path: here,
        declaredUpperBound: node.declaredUpperBound,
        actualCardinality: rows.length,
      });
    }

    // 先把列表挂到结果上，再逐元素填充：深层取消时已完成的前缀
    // 元素作为部分结果保留，而不是整个键消失。
    const items: Array<Record<string, unknown>> = [];
    out[node.alias] = items;
    for (const childRow of rows) {
      // 逐元素检查：兄弟列表元素之间也能及时取消。
      ctx.checkAborted(here.join(' / '));
      const item: Record<string, unknown> = {};
      items.push(item);
      await this.executeFields(
        node.children,
        cn.children,
        childRow,
        rel.to,
        vars,
        ctx,
        here,
        item,
        actuals,
        bump,
        log,
        warnings,
      );
    }
  }
}

function decodeScalar(
  raw: string | number | boolean | null | undefined,
  type: string,
): string | number | boolean | null {
  if (raw === null || raw === undefined) return null;
  if (type === 'bool') return raw === 1 || raw === true;
  return raw;
}

/** 把一棵子树内所有 pending 节点标记为 aborted（未完成解析器标注）。 */
function sweepAborted(node: CostNode): void {
  for (const cn of walk(node)) {
    if (cn.runtimeStatus === 'pending') cn.runtimeStatus = 'aborted';
  }
}

function* walk(node: CostNode): Generator<CostNode> {
  yield node;
  for (const c of node.children) yield* walk(c);
}

/** 用实际发生额回填逐字段成本树。 */
function finalizeActuals(tree: CostNode, actuals: WeakMap<CostNode, ActualTotals>): void {
  const visit = (cn: CostNode): number => {
    let subtree = actuals.get(cn)?.leafCost ?? 0;
    for (const c of cn.children) subtree += visit(c);
    const agg = actuals.get(cn);
    cn.actualSelf = agg?.leafCost ?? 0;
    if (cn.kind === 'list') cn.actualCardinality = agg?.cardinality ?? 0;
    cn.actualSubtree = subtree;
    return subtree;
  };
  visit(tree);
}

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function toDiagnostic(err: unknown): import('../contracts/errors.js').Diagnostic {
  if (err instanceof QueryError) return err.diagnostic;
  return {
    category: 'COMPUTATION_FAILED',
    code: 'STORE_FAILURE',
    message: messageOf(err),
  };
}
