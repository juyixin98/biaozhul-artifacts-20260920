/**
 * Execution kernel (执行内核).
 *
 * Schedules the contract's DAG, propagates one snapshot token and one deadline
 * to every source, enforces "at most one invocation per node", and aggregates
 * partial results with field-level reasons.
 *
 * Scheduling rules:
 *  - a node starts once ALL its dependencies have resolved;
 *  - a failed node skips every transitive dependent (dependency closure), and
 *    those dependents are never invoked;
 *  - a REQUIRED node failure fails the run, yet INDEPENDENT in-flight branches
 *    are allowed to settle so their data is still harvested for diagnostics;
 *  - an OPTIONAL node failure degrades: its fields carry reasons while other
 *    branches keep running;
 *  - the deadline aborts ALL in-flight calls (cooperative signal) — no source
 *    work survives it, and there are no retries (one attempt per node).
 */
import type { CompositeContract, CallSpec, FieldBinding, FieldStatus } from '../contract/types.js';
import type { Source, CallContext } from '../sources/types.js';
import type { SourceRegistry } from '../sources/registry.js';
import type { RunLog } from '../observability/runLog.js';
import {
  computationFailed,
  normalizeThrown,
  type ErrorDetail,
} from './errors.js';
import { abortReason, createDeadline, type DeadlineHandle } from './timing.js';
import type { CompositeResult, ConsistencyLimitation, NodeExecution, NodeState } from './types.js';

export interface ExecuteOptions {
  input: Record<string, string | number>;
  timeoutMs: number;
  snapshotToken: string;
  runId: string;
  log: RunLog;
}

interface NodeRuntime extends NodeExecution {
  spec: CallSpec;
  remainingDeps: number;
  /** Raw source value, fed to dependents' buildRequest. */
  rawValue?: unknown;
}

export class ExecutionKernel {
  constructor(private readonly registry: SourceRegistry) {}

  async execute(contract: CompositeContract, opts: ExecuteOptions): Promise<CompositeResult> {
    const { log, runId } = opts;
    const dl = createDeadline(opts.timeoutMs, runId);
    const nodes = new Map<string, NodeRuntime>();
    for (const spec of contract.calls) {
      nodes.set(spec.id, this.freshNode(spec));
    }

    const ctxBase: Omit<CallContext, 'signal'> = {
      snapshotToken: opts.snapshotToken,
      deadline: dl.deadline,
      timeoutMs: opts.timeoutMs,
      runId,
    };
    log.event('RUN_START', `kernel start contract=${contract.name}`, undefined, {
      timeoutMs: opts.timeoutMs,
      snapshotToken: opts.snapshotToken,
      nodeCount: contract.calls.length,
      input: opts.input,
    });

    const inFlight = new Set<Promise<void>>();
    let terminalFailure: ErrorDetail | null = null;

    const failRun = (detail: ErrorDetail, nodeId: string): void => {
      if (terminalFailure === null) {
        terminalFailure = detail;
        log.decision(nodeId, 'REQUIRED_FAILURE_FAILS_RUN', detail.message, {
          reason: detail.reason,
        });
        // The run is doomed, but in-flight calls on INDEPENDENT branches are
        // allowed to settle so their data can be harvested; nodes blocked on
        // the failed one never unblock and end up skipped. Everything remains
        // bounded by the deadline. No controller abort is forced here.
      }
    };

    const enqueue = (spec: CallSpec): void => {
      const node = nodes.get(spec.id)!;
      if (node.state !== 'pending') return;
      if (dl.controller.signal.aborted) {
        markCancelled(node, abortReason(dl.controller.signal).detail, log);
        return;
      }
      const promise = this.runNode(spec, nodes, ctxBase, dl.controller.signal, log, failRun, enqueue, opts.input);
      inFlight.add(promise);
      void promise.finally(() => inFlight.delete(promise));
    };

    // Initial frontier: nodes without dependencies.
    for (const spec of contract.calls) {
      if (spec.dependencies.length === 0) enqueue(spec);
    }

    // Drain until every scheduled promise has settled.
    while (inFlight.size > 0) {
      await Promise.race(inFlight);
    }
    dl.cleanup();

    return this.assemble(contract, nodes, opts, log, terminalFailure, dl);
  }

  private async runNode(
    spec: CallSpec,
    nodes: Map<string, NodeRuntime>,
    ctxBase: Omit<CallContext, 'signal'>,
    signal: AbortSignal,
    log: RunLog,
    failRun: (detail: ErrorDetail, nodeId: string) => void,
    enqueue: (spec: CallSpec) => void,
    input: Record<string, string | number>,
  ): Promise<void> {
    const node = nodes.get(spec.id)!;
    if (signal.aborted) {
      markCancelled(node, abortReason(signal).detail, log);
      return;
    }
    if (!this.registry.has(spec.source)) {
      const detail = computationFailed('UNKNOWN_SOURCE', `source not registered: ${spec.source}`, {
        context: { source: spec.source },
      }).detail;
      this.finishFailed(node, spec, detail, log, failRun);
      return;
    }

    const source = this.registry.get(spec.source);
    const ctx: CallContext = { ...ctxBase, signal };
    const request = this.buildRequest(spec, nodes, input);
    node.state = 'running';
    node.startedAt = Date.now();
    node.attempts += 1; // exactly one attempt: runNode is invoked once per node
    this.registry.record(spec.source, spec.method, request, ctx);
    log.event('NODE_START', `call ${spec.source}.${spec.method}`, spec.id, {
      deadline: ctx.deadline,
      snapshotToken: ctx.snapshotToken,
      requirement: node.requirement,
      request,
    });

    try {
      const value = await source.call(spec.method, request, ctx);
      const consistency = source.consistency?.(ctx) ?? {
        snapshotHonored: source.capabilities.snapshot,
        dataVersion: source.capabilities.version,
      };
      node.snapshotHonored = consistency.snapshotHonored;
      node.sourceVersion = consistency.dataVersion;
      node.rawValue = value;
      node.value = value;
      node.state = 'resolved';
      node.endedAt = Date.now();
      node.durationMs = node.endedAt - (node.startedAt ?? node.endedAt);
      log.event('NODE_RESOLVED', `${spec.id} resolved in ${node.durationMs}ms`, spec.id, {
        snapshotHonored: node.snapshotHonored,
        dataVersion: node.sourceVersion,
      });
      this.unblockDependents(spec, nodes, enqueue);
    } catch (thrown) {
      node.endedAt = Date.now();
      node.durationMs = node.endedAt - (node.startedAt ?? node.endedAt);
      if (signal.aborted) {
        const reason = abortReason(signal).detail;
        if (reason.reason === 'DEADLINE_EXCEEDED') {
          node.state = 'timeout';
          node.error = reason;
          log.decision(spec.id, 'DEADLINE_ABORT', 'node aborted by propagated deadline', {
            durationMs: node.durationMs,
          });
          if (node.requirement === 'required') failRun(reason, spec.id);
        } else {
          markCancelled(node, reason, log);
        }
      } else {
        this.finishFailed(node, spec, normalizeThrown(thrown), log, failRun);
      }
    }
  }

  private freshNode(spec: CallSpec): NodeRuntime {
    const requirement: NodeExecution['requirement'] =
      spec.fields.length === 0
        ? 'internal'
        : spec.fields.some((f) => f.requirement === 'required')
          ? 'required'
          : 'optional';
    return {
      callId: spec.id,
      source: spec.source,
      method: spec.method,
      requirement,
      state: 'pending',
      startedAt: null,
      endedAt: null,
      durationMs: null,
      attempts: 0,
      dependencyIds: [...spec.dependencies],
      snapshotHonored: true,
      sourceVersion: 'unknown',
      spec,
      remainingDeps: spec.dependencies.length,
    };
  }

  private buildRequest(
    spec: CallSpec,
    nodes: Map<string, NodeRuntime>,
    input: Record<string, string | number>,
  ): unknown {
    const deps: Record<string, unknown> = {};
    for (const dep of spec.dependencies) {
      deps[dep] = nodes.get(dep)?.rawValue;
    }
    return spec.buildRequest(deps, input);
  }

  private unblockDependents(
    resolved: CallSpec,
    nodes: Map<string, NodeRuntime>,
    enqueue: (spec: CallSpec) => void,
  ): void {
    for (const node of nodes.values()) {
      if (node.state !== 'pending' || !node.spec.dependencies.includes(resolved.id)) continue;
      node.remainingDeps -= 1;
      if (node.remainingDeps === 0) enqueue(node.spec);
    }
  }

  private finishFailed(
    node: NodeRuntime,
    spec: CallSpec,
    detail: ErrorDetail,
    log: RunLog,
    failRun: (detail: ErrorDetail, nodeId: string) => void,
  ): void {
    node.state = 'failed';
    node.error = detail;
    log.event('NODE_FAILED', `${spec.id} failed: ${detail.reason}`, spec.id, {
      category: detail.category,
      retryable: detail.retryable,
    });
    if (node.requirement === 'required') {
      failRun(detail, spec.id);
    } else {
      log.decision(
        spec.id,
        'OPTIONAL_FAILURE_DEGRADES',
        'optional node failure degrades to field-level reasons',
        { reason: detail.reason },
      );
    }
  }

  private assemble(
    contract: CompositeContract,
    nodes: Map<string, NodeRuntime>,
    opts: ExecuteOptions,
    log: RunLog,
    terminalFailure: ErrorDetail | null,
    dl: DeadlineHandle,
  ): CompositeResult {
    // Dependents of non-resolved nodes are skipped (transitive closure).
    this.propagateSkips(nodes, log);
    // Anything still pending/running after drain was blocked behind the abort.
    const abortDetail = dl.controller.signal.aborted
      ? abortReason(dl.controller.signal).detail
      : computationFailed('RUN_ABORTED', 'node never ran because the run ended early').detail;
    const deadlineAborted = abortDetail.reason === 'DEADLINE_EXCEEDED';
    for (const node of nodes.values()) {
      if (node.state === 'running') {
        // A call still in flight at drain should already have settled; guard anyway.
        markCancelled(node, abortDetail, log);
      } else if (node.state === 'pending') {
        if (deadlineAborted) {
          node.state = 'timeout';
          node.error = abortDetail;
          log.event('NODE_TIMEOUT_PENDING', `${node.callId} never started before deadline`, node.callId);
        } else {
          markCancelled(node, abortDetail, log);
        }
      }
    }

    const data: Record<string, unknown> = {};
    const fields: FieldStatus[] = [];
    const errors: ErrorDetail[] = [];
    for (const spec of contract.calls) {
      const node = nodes.get(spec.id)!;
      for (const binding of spec.fields) {
        fields.push(this.extractField(binding, node, errors, data, log));
      }
    }
    if (terminalFailure !== null) errors.unshift(terminalFailure);

    const limitations = this.buildLimitations(nodes, log);
    const requiredLost = fields.some((f) => f.requirement === 'required' && f.state !== 'present');
    // A field is 'missing' when the source legitimately produced no value — a
    // declared optional absence keeps the run complete; only a field that
    // FAILED (its node errored/timed out/cancelled) degrades the status.
    const failedFields = fields.some((f) => f.state === 'failed');
    const status: CompositeResult['status'] = requiredLost
      ? 'failed'
      : failedFields
        ? 'partial'
        : 'complete';

    const result: CompositeResult = {
      runId: opts.runId,
      contract: contract.name,
      status,
      snapshotToken: opts.snapshotToken,
      startedAt: dl.startedAt,
      endedAt: Date.now(),
      deadlineMs: opts.timeoutMs,
      data,
      fields,
      nodes: [...nodes.values()].map(stripRuntime),
      limitations,
      errors: dedupeErrors(errors),
    };
    log.event('RUN_END', `kernel end status=${status}`, undefined, {
      status,
      errors: result.errors.map((e) => e.reason),
      limitations: limitations.map((l) => l.type),
      fieldCount: fields.length,
      dataFields: Object.keys(data),
    });
    return result;
  }

  private propagateSkips(nodes: Map<string, NodeRuntime>, log: RunLog): void {
    let changed = true;
    while (changed) {
      changed = false;
      for (const node of nodes.values()) {
        if (node.state !== 'pending') continue;
        const badDep = node.spec.dependencies.find((d) => {
          const state: NodeState = nodes.get(d)!.state;
          return state === 'failed' || state === 'timeout' || state === 'cancelled' || state === 'skipped';
        });
        if (badDep !== undefined) {
          node.state = 'skipped';
          node.error = computationFailed(
            'DEPENDENCY_UNAVAILABLE',
            `node ${node.callId} skipped: dependency ${badDep} did not resolve`,
            { context: { dependency: badDep, upstreamState: nodes.get(badDep)!.state } },
          ).detail;
          log.decision(
            node.callId,
            'DEPENDENCY_SKIP',
            `skipped because ${badDep} was ${nodes.get(badDep)!.state}`,
            { dependency: badDep },
          );
          changed = true;
        }
      }
    }
  }

  private extractField(
    binding: FieldBinding,
    node: NodeRuntime,
    errors: ErrorDetail[],
    data: Record<string, unknown>,
    log: RunLog,
  ): FieldStatus {
    const base: FieldStatus = {
      field: binding.output,
      source: node.source,
      call: node.callId,
      requirement: binding.requirement,
      state: 'missing',
    };

    if (node.state === 'resolved') {
      const value = readPath(node.rawValue, binding.path ?? binding.output);
      // Both `undefined` and explicit `null` mean "the source produced no
      // value for this field".
      if (value !== undefined && value !== null) {
        data[binding.output] = value;
        return { ...base, state: 'present' };
      }
      if (binding.requirement === 'optional' && binding.defaultOnMissing !== undefined) {
        data[binding.output] = binding.defaultOnMissing;
        log.decision(node.callId, 'OPTIONAL_DEFAULT', `${binding.output} absent, default applied`, {
          field: binding.output,
        });
        return { ...base, state: 'present', defaulted: true };
      }
      if (binding.requirement === 'optional') {
        log.decision(node.callId, 'OPTIONAL_MISSING', `${binding.output} absent with no default`, {
          field: binding.output,
        });
        return { ...base, state: 'missing' };
      }
      const detail = computationFailed(
        'REQUIRED_VALUE_MISSING',
        `required field ${binding.output} missing from ${node.source}.${node.method} result`,
      ).detail;
      errors.push(detail);
      log.decision(node.callId, 'REQUIRED_VALUE_MISSING', `${binding.output} absent in resolved value`, {
        field: binding.output,
      });
      return { ...base, state: 'failed', reason: detail };
    }

    const reason =
      node.error ??
      computationFailed('NODE_NOT_RESOLVED', `node ${node.callId} ended as ${node.state}`).detail;
    if (binding.requirement === 'required') {
      errors.push(reason);
      log.decision(node.callId, 'REQUIRED_FIELD_LOST', `${binding.output} unavailable (${node.state})`, {
        field: binding.output,
        nodeState: node.state,
      });
    } else {
      log.decision(node.callId, 'OPTIONAL_FIELD_DEGRADED', `${binding.output} degraded (${node.state})`, {
        field: binding.output,
        nodeState: node.state,
      });
    }
    return { ...base, state: 'failed', reason };
  }

  private buildLimitations(nodes: Map<string, NodeRuntime>, log: RunLog): ConsistencyLimitation[] {
    const limitations: ConsistencyLimitation[] = [];
    const contacted = [...nodes.values()].filter(
      (n) => n.state === 'resolved' || n.state === 'failed' || n.state === 'timeout',
    );

    const noSnapshot = [...new Set(contacted.filter((n) => !n.snapshotHonored).map((n) => n.source))];
    if (noSnapshot.length > 0) {
      limitations.push({
        type: 'SNAPSHOT_UNSUPPORTED',
        sources: noSnapshot,
        detail: 'these sources cannot honor the request snapshot token; reads are not pinned',
      });
      log.decision(
        noSnapshot.join(','),
        'SNAPSHOT_LIMITATION',
        'source cannot pin the snapshot token; recorded as consistency limitation',
        { sources: noSnapshot },
      );
    }

    const versions = new Map<string, string>();
    for (const n of contacted) {
      // Versions are only comparable across sources that actually pinned the
      // same snapshot token; a live-read legacy source cannot participate.
      if (n.snapshotHonored && n.sourceVersion && n.sourceVersion !== 'unknown') {
        versions.set(n.source, n.sourceVersion);
      }
    }
    const distinct = [...new Set(versions.values())];
    if (distinct.length > 1) {
      limitations.push({
        type: 'VERSION_MISMATCH',
        sources: [...versions.keys()],
        detail: `sources served different data versions under one snapshot: ${distinct.join(' vs ')}`,
        versions: Object.fromEntries(versions),
      });
      log.decision(
        [...versions.keys()].join(','),
        'VERSION_SKEW_LIMITATION',
        'snapshot-capable sources served different data versions',
        { versions: Object.fromEntries(versions) },
      );
    }
    return limitations;
  }
}

function markCancelled(node: NodeRuntime, reason: ErrorDetail, log: RunLog): void {
  if (node.state === 'cancelled' || node.state === 'skipped' || node.state === 'resolved') return;
  node.state = 'cancelled';
  node.endedAt = Date.now();
  node.error = reason;
  log.event('NODE_CANCELLED', `${node.callId} cancelled (${reason.reason})`, node.callId);
}

function readPath(value: unknown, path: string): unknown {
  return path.split('.').reduce<unknown>((acc, key) => {
    if (acc === null || acc === undefined || typeof acc !== 'object') return undefined;
    return (acc as Record<string, unknown>)[key];
  }, value);
}

function stripRuntime(node: NodeRuntime): NodeExecution {
  const { spec: _spec, remainingDeps: _remainingDeps, rawValue: _rawValue, ...rest } = node;
  return rest;
}

function dedupeErrors(errors: ErrorDetail[]): ErrorDetail[] {
  const seen = new Set<string>();
  const out: ErrorDetail[] = [];
  for (const e of errors) {
    const key = `${e.reason}:${e.message}`;
    if (!seen.has(key)) {
      seen.add(key);
      out.push(e);
    }
  }
  return out;
}
