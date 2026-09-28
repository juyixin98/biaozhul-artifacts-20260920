import type { FailureCategory } from '../errors.js';
import type { FieldResult, NodeStatus } from '../types.js';

/**
 * Kernel event stream. Every state transition of a run is emitted as one of
 * these events and persisted (SQLite `run_events` + optional JSONL file),
 * giving an ordered, replayable trace keyed by runId.
 */
export type KernelEvent =
  | {
      seq: number;
      ts: number;
      runId: string;
      type: 'run-started';
      contract: string;
      deadline: number;
      snapshotToken: string | null;
      params: Record<string, unknown>;
    }
  | { seq: number; ts: number; runId: string; type: 'node-ready'; nodeId: string }
  | {
      seq: number;
      ts: number;
      runId: string;
      type: 'node-skipped';
      nodeId: string;
      reason: string;
      upstreamNodeId?: string;
      category: FailureCategory;
    }
  | {
      seq: number;
      ts: number;
      runId: string;
      type: 'node-invoked';
      nodeId: string;
      source: string;
      deadline: number;
      snapshotToken: string | null;
      attempts: number;
    }
  | {
      seq: number;
      ts: number;
      runId: string;
      type: 'node-settled';
      nodeId: string;
      status: NodeStatus;
      latencyMs: number;
      category?: FailureCategory;
      code?: string;
    }
  | {
      seq: number;
      ts: number;
      runId: string;
      type: 'deadline-fired';
      scope: 'run' | 'node';
      nodeId?: string;
    }
  | {
      seq: number;
      ts: number;
      runId: string;
      type: 'late-settle-ignored';
      nodeId: string;
      status: NodeStatus;
    }
  | { seq: number; ts: number; runId: string; type: 'field-resolved'; field: FieldResult }
  | {
      seq: number;
      ts: number;
      runId: string;
      type: 'run-finished';
      outcome: string;
      durationMs: number;
      reason: string;
    };

type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never;

/** An event before the collector assigns its global sequence number. */
export type EmittableEvent = DistributiveOmit<KernelEvent, 'seq'>;

export interface EventSink {
  record(event: KernelEvent): void;
}

/** Collects events in memory and optionally mirrors them as JSONL lines. */
export class EventCollector implements EventSink {
  private seq = 0;
  readonly events: KernelEvent[] = [];

  constructor(private readonly jsonlWrite?: (line: string) => void) {}

  record(event: KernelEvent): void {
    // Seq is assigned by the collector to keep a total order even when events
    // from concurrent nodes interleave.
    const ordered = { ...event, seq: this.seq++ };
    this.events.push(ordered);
    this.jsonlWrite?.(JSON.stringify(ordered));
  }
}
