import { randomUUID } from 'node:crypto';
import { DomainError, type FailureDetail } from '../errors.js';
import { nodeDependencies, topoSort } from '../contract/parser.js';
import { BoundedSemaphore } from './semaphore.js';
import { EventCollector, type EmittableEvent, type EventSink, type KernelEvent } from './events.js';
import { FieldAssembler } from './fieldAssembler.js';
import type { NodeRuntime } from './runtime.js';
import type { RunStore } from '../state/runStore.js';
import type {
  CompositeContract,
  CompositeRequest,
  CompositeResponse,
  ConsistencyNote,
  DataSource,
  FieldResult,
  NodeDeclaration,
  NodeExecutionRecord,
  NodeStatus,
  ParamValue,
  SourceResult,
} from '../types.js';

const DEFAULT_RUN_TIMEOUT_MS = 2000;
/** setTimeout safety clamp. */
const TIMER_CLAMP_MS = 2_147_000_000;

export interface KernelOptions {
  runId?: string;
  timeoutMs?: number;
  snapshotToken?: string | null;
  params?: Record<string, unknown>;
}

export interface KernelDeps {
  sources: Map<string, DataSource>;
  semaphore: BoundedSemaphore;
  store?: RunStore;
  sink?: EventSink;
  clock?: () => number;
}

/**
 * Execution kernel.
 *
 * Responsibilities (phase 1 boundaries):
 *  - schedule the parsed dependency DAG (diamond nodes execute exactly once)
 *  - propagate the same snapshot token and an absolute deadline to every source
 *  - abort in-flight work at the deadline (no unbounded background work)
 *  - distinguish required failures from optional defaults / missing values
 *  - assemble the typed response with per-field provenance and reasons
 */
export class CompositeKernel {
  private readonly clock: () => number;

  constructor(private readonly deps: KernelDeps) {
    this.clock = deps.clock ?? (() => Date.now());
  }

  async run(contract: CompositeContract, options: KernelOptions = {}): Promise<CompositeResponse> {
    const startedAt = this.clock();
    const timeoutMs = options.timeoutMs ?? DEFAULT_RUN_TIMEOUT_MS;
    const deadline = startedAt + timeoutMs;
    const runId = options.runId ?? `run-${randomUUID()}`;
    const requestParams = options.params ?? {};
    const snapshotToken = options.snapshotToken ?? null;
    const requestedVersion = parseSnapshotVersion(snapshotToken);

    const request: CompositeRequest = { runId, snapshotToken, deadline, params: requestParams };

    this.validateRequestParams(contract, requestParams);

    // Persist run row before events so event FKs always resolve.
    this.deps.store?.startRun({ runId, contract, snapshotToken, startedAt, deadline, requestParams });

    const collector = new EventCollector();
    const forward = (event: EmittableEvent): void => {
      collector.record(event as KernelEvent);
      const stored = collector.events[collector.events.length - 1]!;
      this.deps.sink?.record(stored);
      this.deps.store?.recordEvent(stored);
    };

    forward({
      ts: startedAt,
      runId,
      type: 'run-started',
      contract: `${contract.name}@v${contract.version}`,
      deadline,
      snapshotToken,
      params: requestParams,
    });

    /* ---------------- run-wide deadline / cancellation ---------------- */
    const runController = new AbortController();
    let runDeadlineFired = false;
    const runTimer = setTimeout(() => {
      runDeadlineFired = true;
      forward({ ts: this.clock(), runId, type: 'deadline-fired', scope: 'run' });
      runController.abort(
        new DomainError({
          category: 'CANCELLED',
          code: 'RUN_DEADLINE_EXCEEDED',
          message: `composite run ${runId} exceeded its ${timeoutMs}ms deadline`,
          httpStatus: 504,
          retryable: true,
        }),
      );
    }, clampDelay(deadline - this.clock()));

    const order = topoSort(contract);
    const nodesById = new Map(contract.nodes.map((n) => [n.id, n]));
    const runtimes = new Map<string, NodeRuntime>();

    let schedulerFailure: Error | undefined;

    const ensureNode = (nodeId: string): Promise<void> => {
      const existing = runtimes.get(nodeId);
      if (existing) return existing.promise;
      const decl = nodesById.get(nodeId);
      if (!decl) {
        return Promise.reject(
          new DomainError({
            category: 'COMPUTATION_FAILED',
            code: 'UNKNOWN_NODE',
            message: `scheduler referenced unknown node ${nodeId}`,
            httpStatus: 500,
          }),
        );
      }
      const rt = this.createRuntime(decl, snapshotToken, deadline);
      rt.promise = executeNode(rt).catch((error) => {
        // Defensive: executeNode is expected to classify everything. An
        // unexpected escape fails the whole run rather than hanging it.
        schedulerFailure = error as Error;
      });
      runtimes.set(nodeId, rt);
      return rt.promise;
    };

    const settle = (rt: NodeRuntime, status: NodeStatus, failure?: DomainError): void => {
      const now = this.clock();
      rt.record.status = status;
      rt.record.startedAt ??= now;
      rt.record.finishedAt = now;
      rt.record.latencyMs = now - rt.record.startedAt;
      if (failure) rt.record.failure = failure.toFailure(`node:${rt.decl.id}`);
      forward({
        ts: now,
        runId,
        type: 'node-settled',
        nodeId: rt.decl.id,
        status,
        latencyMs: rt.record.latencyMs,
        category: failure?.category,
        code: failure?.code,
      });
    };

    const executeNode = async (rt: NodeRuntime): Promise<void> => {
      const decl = rt.decl;
      forward({ ts: this.clock(), runId, type: 'node-ready', nodeId: decl.id });

      // 1) Resolve REQUIRED dependencies (memoized; diamond joins share one
      //    invocation). Optional dependencies are awaited only if they have
      //    already settled — a slow optional must not block its downstream;
      //    its param reference resolves to its declared default instead.
      const depIds = nodeDependencies(decl);
      const requiredIds = [...requiredDependencies(decl)];
      await Promise.all(requiredIds.map((id) => ensureNode(id)));
      const depRuntimes = depIds.map((id) => runtimes.get(id)!);

      // 2) Dependency gate FIRST: a failed/timed-out/cancelled REQUIRED
      //    upstream makes params unresolvable. Classifying this as "skipped"
      //    keeps the causal chain even when the run deadline also fired.
      //    Optional param references carry a default instead (step 6), so an
      //    optional upstream never blocks invocation.
      const requiredDepIds = requiredDependencies(decl);
      const blocking = depRuntimes.find(
        (dep) =>
          requiredDepIds.has(dep.decl.id) &&
          (dep.record.status === 'failed' ||
            dep.record.status === 'timed-out' ||
            dep.record.status === 'cancelled' ||
            dep.record.status === 'skipped'),
      );
      if (blocking) {
        rt.record.didNotInvoke = true;
        settle(rt, 'skipped', this.upstreamError(decl, blocking));
        return;
      }

      // 3) Hard gate: run deadline fired before this node could start.
      if (runDeadlineFired || runController.signal.aborted) {
        rt.record.didNotInvoke = true;
        settle(rt, 'cancelled', this.runDeadlineError(decl));
        return;
      }

      // 4) Bounded concurrency slot; the queue wait is itself abortable.
      const nodeController = new AbortController();
      const signal = AbortSignal.any([runController.signal, nodeController.signal]);
      let release: (() => void) | undefined;
      try {
        release = await this.deps.semaphore.acquire(decl.id, signal);
      } catch (error) {
        rt.record.didNotInvoke = true;
        if (runDeadlineFired || signal.aborted) {
          settle(rt, runDeadlineFired ? 'cancelled' : 'timed-out', this.deadlineError(decl, runDeadlineFired));
        } else {
          settle(rt, 'failed', error as DomainError);
        }
        return;
      }

      try {
        if (runDeadlineFired || signal.aborted) {
          rt.record.didNotInvoke = true;
          settle(rt, runDeadlineFired ? 'cancelled' : 'timed-out', this.deadlineError(decl, runDeadlineFired));
          return;
        }

        // 5) Deadline propagation: effective = min(run deadline, node budget).
        const now = this.clock();
        const effectiveDeadline = decl.timeoutMs
          ? Math.min(request.deadline, now + decl.timeoutMs)
          : request.deadline;
        rt.record.deadline = effectiveDeadline;
        const waitMs = effectiveDeadline - now;
        if (waitMs <= 0) {
          rt.record.didNotInvoke = true;
          settle(rt, runDeadlineFired ? 'cancelled' : 'timed-out', this.deadlineError(decl, runDeadlineFired));
          return;
        }

        // 6) Concrete params from literals / request input / upstream records.
        let params: Record<string, unknown>;
        try {
          params = this.resolveParams(decl, requestParams, depRuntimes);
        } catch (error) {
          settle(rt, 'failed', error as DomainError);
          return;
        }

        // 7) Invoke the source exactly once, racing the propagated deadline.
        const source = this.deps.sources.get(decl.source);
        if (!source) {
          settle(
            rt,
            'failed',
            new DomainError({
              category: 'COMPUTATION_FAILED',
              code: 'SOURCE_NOT_REGISTERED',
              message: `node ${decl.id}: source "${decl.source}" is not registered`,
              httpStatus: 500,
              details: { source: decl.source },
            }),
          );
          return;
        }

        // Install a node-local timer ONLY when the node's budget is
        // strictly tighter than the run deadline. If they are equal, the
        // run timer alone owns cancellation — two timers for the same
        // instant race, and the node timer clearing the run timer would
        // swallow the run-level deadline event.
        const nodeBudgetIsTighter = effectiveDeadline < request.deadline;
        let nodeTimerFired = false;
        let nodeTimer: ReturnType<typeof setTimeout> | undefined;
        if (nodeBudgetIsTighter) {
          nodeTimer = setTimeout(() => {
            nodeTimerFired = true;
            forward({ ts: this.clock(), runId, type: 'deadline-fired', scope: 'node', nodeId: decl.id });
            nodeController.abort(
              new DomainError({
                category: 'SOURCE_TIMEOUT',
                code: 'NODE_DEADLINE_EXCEEDED',
                message: `node ${decl.id} exceeded its ${decl.timeoutMs}ms node deadline`,
                httpStatus: 504,
                retryable: true,
              }),
            );
          }, clampDelay(waitMs));
        }

        rt.record.status = 'running';
        rt.record.attempts = 1;
        rt.record.startedAt = this.clock();
        forward({
          ts: rt.record.startedAt,
          runId,
          type: 'node-invoked',
          nodeId: decl.id,
          source: decl.source,
          deadline: effectiveDeadline,
          snapshotToken,
          attempts: 1,
        });

        try {
          const result = await Promise.race([
            source.fetch({ params, snapshotToken, deadline: effectiveDeadline, signal }),
            abortPromise(signal),
          ]);
          rt.result = result;
          rt.record.result = result;
          rt.record.snapshotHonoured = this.wasSnapshotHonoured(result, requestedVersion);
          settle(rt, 'succeeded');
        } catch (error) {
          if (signal.aborted) {
            // A node only times out when its own (tighter) timer fired;
            // cancellation at an effective deadline == run deadline is owned
            // by the run timer and classifies as CANCELLED.
            const timedOut = nodeBudgetIsTighter && nodeTimerFired && !runDeadlineFired;
            forward({
              ts: this.clock(),
              runId,
              type: 'late-settle-ignored',
              nodeId: decl.id,
              status: timedOut ? 'timed-out' : 'cancelled',
            });
            settle(
              rt,
              timedOut ? 'timed-out' : 'cancelled',
              this.deadlineError(decl, !timedOut),
            );
          } else if (error instanceof DomainError) {
            settle(rt, 'failed', error);
          } else {
            settle(
              rt,
              'failed',
              new DomainError({
                category: 'SOURCE_FAILURE',
                code: 'SOURCE_THREW',
                message: `node ${decl.id}: source "${decl.source}" threw: ${(error as Error).message}`,
                httpStatus: 502,
                causedBy: (error as Error).name,
              }),
            );
          }
        } finally {
          clearTimeout(nodeTimer);
        }
      } finally {
        release?.();
      }
    };

    /* ----------------------------- run graph ----------------------------- */
    let runError: DomainError | undefined;
    try {
      await Promise.all(order.map((id) => ensureNode(id)));
      if (schedulerFailure) throw schedulerFailure;
    } catch (error) {
      runError =
        error instanceof DomainError
          ? error
          : new DomainError({
              category: 'COMPUTATION_FAILED',
              code: 'SCHEDULER_ERROR',
              message: `scheduler failed: ${(error as Error).message}`,
              httpStatus: 500,
            });
    } finally {
      clearTimeout(runTimer);
    }

    /* ---------------- snapshot consistency aggregation ---------------- */
    const consistency: ConsistencyNote[] = [];
    this.collectConsistency(contract, runtimes, request, requestedVersion, consistency, settle, forward);

    /* --------------------------- field assembly -------------------------- */
    const fields = new FieldAssembler(this.clock, forward).assemble(
      contract,
      runtimes,
      runId,
    );
    const data: Record<string, unknown> = {};
    for (const field of fields) {
      if (field.status === 'present' || field.status === 'missing-optional-default') {
        setDeepValue(data, field.path, field.value ?? null);
      }
    }

    const requiredMissing = fields.filter(
      (f) =>
        f.required &&
        f.status !== 'present' &&
        f.status !== 'missing-optional-default',
    );
    const outcome: CompositeResponse['outcome'] = runError
      ? 'failed'
      : requiredMissing.length === 0
        ? 'complete'
        : 'partial';

    const failures = this.collectFailures(runtimes, fields, runError);

    const observedVersions = [
      ...new Set(
        [...runtimes.values()]
          .filter((rt) => rt.record.status === 'succeeded')
          .map((rt) => rt.result?.meta.dataVersion)
          .filter((v): v is string => typeof v === 'string'),
      ),
    ].sort();

    const finishedAt = this.clock();
    const response: CompositeResponse = {
      runId,
      outcome,
      contract: { name: contract.name, version: contract.version },
      snapshotToken,
      dataVersion: {
        requested: requestedVersion,
        observed: observedVersions,
        consistent: observedVersions.length <= 1,
      },
      data,
      fields,
      nodeResults: order.map((id) => runtimes.get(id)!.record),
      consistency,
      failures,
      startedAt,
      finishedAt,
      durationMs: finishedAt - startedAt,
    };

    const reason = runError
      ? `run-error:${runError.code}`
      : outcome === 'complete'
        ? 'all-required-fields-present'
        : `required-fields-missing:${requiredMissing.map((f) => f.path).join(',')}`;
    forward({ ts: finishedAt, runId, type: 'run-finished', outcome, durationMs: response.durationMs, reason });

    this.deps.store?.persist(response, contract, requestParams, response.nodeResults);
    return response;
  }

  /* ====================================================================== */

  private createRuntime(
    decl: NodeDeclaration,
    snapshotToken: string | null,
    deadline: number,
  ): NodeRuntime {
    return {
      decl,
      record: {
        nodeId: decl.id,
        source: decl.source,
        status: 'pending',
        attempts: 0,
        snapshotTokenUsed: snapshotToken,
        snapshotHonoured: false,
        deadline,
        didNotInvoke: false,
      },
      promise: Promise.resolve(),
    };
  }

  private wasSnapshotHonoured(result: SourceResult, requestedVersion: string | null): boolean {
    if (!result.meta.supportsSnapshot) return false;
    // A source that advertises snapshot support but serves a different
    // version than the token encodes has not in fact honoured it.
    return requestedVersion === null || result.meta.dataVersion === requestedVersion;
  }

  private runDeadlineError(decl: NodeDeclaration): DomainError {
    return new DomainError({
      category: 'CANCELLED',
      code: 'RUN_DEADLINE_EXCEEDED',
      message: `node ${decl.id} not started: run deadline already passed`,
      httpStatus: 504,
      retryable: true,
    });
  }

  private deadlineError(decl: NodeDeclaration, runFired: boolean): DomainError {
    return runFired
      ? new DomainError({
          category: 'CANCELLED',
          code: 'RUN_DEADLINE_EXCEEDED',
          message: `node ${decl.id} cancelled by propagated run deadline`,
          httpStatus: 504,
          retryable: true,
        })
      : new DomainError({
          category: 'SOURCE_TIMEOUT',
          code: 'NODE_DEADLINE_EXCEEDED',
          message: `node ${decl.id} exceeded its propagated deadline; source cancelled`,
          httpStatus: 504,
          retryable: true,
        });
  }

  private upstreamError(
    decl: NodeDeclaration,
    blocking: NodeRuntime,
  ): DomainError {
    return new DomainError({
      category: blocking.record.failure?.category ?? 'SOURCE_FAILURE',
      code: 'UPSTREAM_DEPENDENCY_FAILED',
      message: `node ${decl.id} skipped: dependency ${blocking.decl.id} is ${blocking.record.status}`,
      httpStatus: 502,
      causedBy: blocking.record.failure?.code,
      details: {
        upstreamNode: blocking.decl.id,
        upstreamStatus: blocking.record.status,
        thisNode: decl.id,
        thisNecessity: decl.required,
      },
    });
  }

  private validateRequestParams(
    contract: CompositeContract,
    params: Record<string, unknown>,
  ): void {
    const referenced = new Set<string>();
    for (const node of contract.nodes) {
      for (const param of Object.values(node.params)) {
        if (param.kind === 'request') referenced.add(param.key);
      }
    }
    const missing = [...referenced].filter((key) => params[key] === undefined);
    if (missing.length > 0) {
      throw new DomainError({
        category: 'MISSING_INPUT',
        code: 'MISSING_REQUEST_PARAM',
        message: `missing required request parameter(s): ${missing.join(', ')}`,
        httpStatus: 400,
        details: { missing },
      });
    }
  }

  private resolveParams(
    decl: NodeDeclaration,
    requestParams: Record<string, unknown>,
    deps: NodeRuntime[],
  ): Record<string, unknown> {
    // Only succeeded nodes carry records; optional references to missing
    // records are defaulted in resolveOneParam.
    const records = new Map(
      deps
        .filter((d) => d.result !== undefined)
        .map((d) => [d.decl.id, d.result!.record]),
    );
    const resolved: Record<string, unknown> = {};
    for (const [key, param] of Object.entries(decl.params)) {
      resolved[key] = this.resolveOneParam(decl, key, param, requestParams, records);
    }
    return resolved;
  }

  private resolveOneParam(
    decl: NodeDeclaration,
    key: string,
    param: ParamValue,
    requestParams: Record<string, unknown>,
    records: Map<string, Record<string, unknown>>,
  ): unknown {
    if (param.kind === 'literal') return param.value;
    if (param.kind === 'request') return requestParams[param.key];

    const record = records.get(param.fromNode);
    const unavailable =
      !record ||
      (param.property !== undefined && !(param.property in record));

    if (unavailable) {
      if (param.kind === 'ref' && param.optional === true) {
        return param.default;
      }
      if (!record) {
        throw new DomainError({
          category: 'COMPUTATION_FAILED',
          code: 'UPSTREAM_RECORD_UNAVAILABLE',
          message: `node ${decl.id} param ${key}: upstream record ${param.fromNode} unavailable`,
          httpStatus: 500,
        });
      }
      throw new DomainError({
        category: 'SOURCE_FAILURE',
        code: 'UPSTREAM_PROPERTY_MISSING',
        message: `node ${decl.id} param ${key}: property "${param.property}" missing on ${param.fromNode} result`,
        httpStatus: 502,
        details: { upstreamNode: param.fromNode, property: param.property },
      });
    }
    if (param.property === undefined) return record;
    return record[param.property];
  }

  private collectConsistency(
    contract: CompositeContract,
    runtimes: Map<string, NodeRuntime>,
    request: CompositeRequest,
    requestedVersion: string | null,
    consistency: ConsistencyNote[],
    settle: (rt: NodeRuntime, status: NodeStatus, failure?: DomainError) => void,
    forward: (event: EmittableEvent) => void,
  ): void {
    if (request.snapshotToken === null) {
      for (const rt of runtimes.values()) {
        if (rt.record.status === 'succeeded') {
          consistency.push({
            kind: 'snapshot-not-requested',
            nodeId: rt.decl.id,
            source: rt.decl.source,
            detail: 'no snapshot token in request; source served latest data',
          });
        }
      }
      return;
    }

    const conflicted = new Set<string>();
    for (const rt of runtimes.values()) {
      if (rt.record.status !== 'succeeded') continue;
      const meta = rt.result!.meta;
      const requirement = rt.decl.snapshotRequirement ?? 'best-effort';

      if (!meta.supportsSnapshot) {
        if (requirement === 'exact') {
          conflicted.add(rt.decl.id);
          settle(
            rt,
            'failed',
            new DomainError({
              category: 'STATE_CONFLICT',
              code: 'SNAPSHOT_NOT_SUPPORTED',
              message: `node ${rt.decl.id} requires an exact snapshot but source "${rt.decl.source}" cannot honour token ${request.snapshotToken}`,
              httpStatus: 409,
              details: { nodeId: rt.decl.id, source: rt.decl.source },
            }),
          );
        } else {
          rt.record.snapshotHonoured = false;
          consistency.push({
            kind: 'snapshot-unsupported',
            nodeId: rt.decl.id,
            source: rt.decl.source,
            detail: `source does not support snapshots; served dataVersion ${meta.dataVersion} (consistency limited)`,
          });
        }
        continue;
      }

      if (requestedVersion !== null && meta.dataVersion !== requestedVersion) {
        if (requirement === 'exact') {
          conflicted.add(rt.decl.id);
          settle(
            rt,
            'failed',
            new DomainError({
              category: 'STATE_CONFLICT',
              code: 'SNAPSHOT_VERSION_DRIFT',
              message: `node ${rt.decl.id} served ${meta.dataVersion} but token requires ${requestedVersion}`,
              httpStatus: 409,
              details: { nodeId: rt.decl.id, observed: meta.dataVersion, requested: requestedVersion },
            }),
          );
        } else {
          consistency.push({
            kind: 'snapshot-mismatch',
            nodeId: rt.decl.id,
            source: rt.decl.source,
            detail: `source served ${meta.dataVersion}, token requests ${requestedVersion} (best-effort; consistency limited)`,
          });
        }
      }
    }

    // Downstream nodes consumed data from a now-conflicted node: annotate.
    if (conflicted.size > 0) {
      const downstream = this.transitiveDependents(contract, conflicted);
      for (const id of downstream) {
        const rt = runtimes.get(id);
        if (rt && rt.record.status === 'succeeded') {
          consistency.push({
            kind: 'snapshot-mismatch',
            nodeId: id,
            source: rt.decl.source,
            detail: `node consumed data transitively derived from exact-snapshot conflict at ${[...conflicted].join(',')}`,
          });
        }
      }
    }
    void forward;
  }

  private transitiveDependents(contract: CompositeContract, roots: Set<string>): Set<string> {
    const dependentsOf = new Map<string, string[]>();
    for (const node of contract.nodes) {
      for (const dep of nodeDependencies(node)) {
        const list = dependentsOf.get(dep) ?? [];
        list.push(node.id);
        dependentsOf.set(dep, list);
      }
    }
    const result = new Set<string>();
    const queue = [...roots];
    while (queue.length > 0) {
      const id = queue.shift()!;
      for (const dependent of dependentsOf.get(id) ?? []) {
        if (!result.has(dependent) && !roots.has(dependent)) {
          result.add(dependent);
          queue.push(dependent);
        }
      }
    }
    return result;
  }

  private collectFailures(
    runtimes: Map<string, NodeRuntime>,
    fields: FieldResult[],
    runError?: DomainError,
  ): FailureDetail[] {
    const seen = new Set<string>();
    const failures: FailureDetail[] = [];
    const add = (detail: FailureDetail): void => {
      const key = `${detail.at}|${detail.code}|${detail.category}`;
      if (!seen.has(key)) {
        seen.add(key);
        failures.push(detail);
      }
    };

    if (runError) add(runError.toFailure('run'));

    const terminal: NodeStatus[] = ['failed', 'timed-out', 'cancelled', 'skipped'];
    for (const rt of runtimes.values()) {
      if (terminal.includes(rt.record.status) && rt.record.failure) {
        add(rt.record.failure);
      }
    }
    for (const field of fields) {
      for (const reason of field.reasons) add(reason);
    }
    return failures;
  }
}

/* ------------------------------- helpers -------------------------------- */

function abortPromise(signal: AbortSignal): Promise<never> {
  return new Promise((_, reject) => {
    if (signal.aborted) {
      reject(signal.reason instanceof Error ? signal.reason : new Error('aborted'));
      return;
    }
    signal.addEventListener(
      'abort',
      () => reject(signal.reason instanceof Error ? signal.reason : new Error('aborted')),
      { once: true },
    );
  });
}

function clampDelay(ms: number): number {
  return Math.min(Math.max(0, ms), TIMER_CLAMP_MS);
}

/** Node dependency ids whose param reference is required (blocks invocation). */
function requiredDependencies(decl: NodeDeclaration): Set<string> {
  const ids = new Set<string>();
  for (const param of Object.values(decl.params)) {
    if (param.kind === 'ref' && param.optional !== true) ids.add(param.fromNode);
  }
  return ids;
}

function parseSnapshotVersion(token: string | null): string | null {
  if (!token) return null;
  const match = /^snap-(v[\w.-]+)-/.exec(token);
  return match ? (match[1] ?? null) : null;
}

function setDeepValue(target: Record<string, unknown>, path: string, value: unknown): void {
  const parts = path.split('.');
  let cursor = target;
  for (let i = 0; i < parts.length - 1; i += 1) {
    const key = parts[i]!;
    const next = cursor[key];
    if (typeof next !== 'object' || next === null || Array.isArray(next)) {
      cursor[key] = {};
    }
    cursor = cursor[key] as Record<string, unknown>;
  }
  cursor[parts[parts.length - 1]!] = value;
}
