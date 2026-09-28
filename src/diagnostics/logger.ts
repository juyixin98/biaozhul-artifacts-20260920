import type { JsonValue } from '../contract/protocol.js';
import type {
  AppendEventArgs,
  DiagnosticDecision,
  DiagnosticEvent,
  StateStore,
} from '../state/store.js';
import { redact } from './redact.js';

export type LogLevel = 'silent' | 'info' | 'debug';

/**
 * Diagnostic recorder. Every decision (accept / reject / indeterminate) gets
 * a durable event with correlation ids (connection, batch, rpc id, operation)
 * and a machine-readable reason. Nothing sensitive is ever written: detail is
 * redacted before persistence AND before console output.
 */
export class DiagnosticLogger {
  constructor(
    private readonly store: StateStore,
    private level: LogLevel = 'info',
    private readonly sink: (line: string) => void = (line) => process.stderr.write(`${line}\n`),
  ) {}

  record(args: AppendEventArgs): DiagnosticEvent {
    const event = this.store.appendEvent({
      ...args,
      detail: args.detail === undefined ? null : (redact(args.detail) ?? null),
    });
    this.emit(event);
    return event;
  }

  accepted(
    kind: string,
    reason: string,
    ids: CorrelationIds,
    detail?: Record<string, unknown>,
  ): DiagnosticEvent {
    return this.record({
      kind,
      decision: 'accepted',
      reason,
      ...ids,
      detail: detail as JsonValue | undefined,
    });
  }

  rejected(
    kind: string,
    reason: string,
    ids: CorrelationIds,
    detail?: Record<string, unknown>,
  ): DiagnosticEvent {
    return this.record({
      kind,
      decision: 'rejected',
      reason,
      ...ids,
      detail: detail as JsonValue | undefined,
    });
  }

  indeterminate(
    kind: string,
    reason: string,
    ids: CorrelationIds,
    detail?: Record<string, unknown>,
  ): DiagnosticEvent {
    return this.record({
      kind,
      decision: 'indeterminate',
      reason,
      ...ids,
      detail: detail as JsonValue | undefined,
    });
  }

  private emit(event: DiagnosticEvent): void {
    if (this.level === 'silent') return;
    const decisionTag: Record<DiagnosticDecision, string> = {
      accepted: 'ACCEPT ',
      rejected: 'REJECT ',
      indeterminate: 'UNKWN  ',
    };
    const base = `${decisionTag[event.decision]} ${event.kind} reason=${event.reason}`;
    const corr = [
      event.connectionId ? `conn=${event.connectionId}` : null,
      event.batchId ? `batch=${event.batchId}` : null,
      event.rpcId ? `rpc=${event.rpcId}` : null,
      event.operationId ? `op=${event.operationId}` : null,
    ]
      .filter(Boolean)
      .join(' ');
    const debugDetail =
      this.level === 'debug' && event.detail !== null ? ` detail=${JSON.stringify(event.detail)}` : '';
    this.sink(`${base} ${corr}${debugDetail}`.trimEnd());
  }
}

export interface CorrelationIds {
  connectionId?: string | null;
  batchId?: string | null;
  rpcId?: string | null;
  operationId?: string | null;
}
