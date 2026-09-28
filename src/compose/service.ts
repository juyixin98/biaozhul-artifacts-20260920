/**
 * Compose service — application facade.
 *
 * Wires the four boundaries together:
 *   contract parsing/validation -> execution kernel -> state adapter -> log,
 * and enforces request-level rules (timeout bounds, snapshot token generation,
 * idempotency replay).
 */
import { randomUUID } from 'node:crypto';
import { validateContract, validateInput } from '../contract/validate.js';
import { ExecutionKernel } from '../kernel/executor.js';
import { inputError } from '../kernel/errors.js';
import { RunLog, jsonlFileSink } from '../observability/runLog.js';
import { RunStore } from '../state/runStore.js';
import { contracts } from './contracts.js';
import { getScenario } from './scenarios.js';
import type { AppConfig } from '../config/index.js';
import type { CompositeResult } from '../kernel/types.js';

export interface ComposeRequest {
  contract: string;
  input: Record<string, unknown>;
  scenario?: string;
  timeoutMs?: number;
  snapshotToken?: string;
  idempotencyKey?: string;
}

export class ComposeService {
  constructor(
    private readonly store: RunStore,
    private readonly config: AppConfig,
  ) {
    for (const contract of Object.values(contracts)) {
      validateContract(contract);
    }
  }

  async run(request: ComposeRequest): Promise<{ result: CompositeResult; replayed: boolean }> {
    const contract = contracts[request.contract];
    if (!contract) {
      throw inputError('UNKNOWN_CONTRACT', `unknown contract: ${request.contract}`, {
        known: Object.keys(contracts),
      });
    }

    const timeoutMs = this.clampTimeout(request.timeoutMs);
    const input = validateInput(contract, request.input ?? {});
    const snapshotToken = request.snapshotToken ?? `snap-${randomUUID()}`;
    if (typeof snapshotToken !== 'string' || snapshotToken.length === 0) {
      throw inputError('INVALID_SNAPSHOT_TOKEN', 'snapshotToken must be a non-empty string');
    }

    // The idempotency fingerprint binds client-controlled request semantics.
    // An explicit client snapshot token is part of the requested read view;
    // a server-generated token is omitted so identical logical requests replay.
    const fingerprintPayload = JSON.stringify({
      contract: request.contract,
      input,
      scenario: request.scenario ?? null,
      timeoutMs,
      ...(request.snapshotToken !== undefined ? { snapshotToken } : {}),
    });
    const runId = `run-${Date.now().toString(36)}-${randomUUID().slice(0, 8)}`;

    if (request.idempotencyKey) {
      const reservation = this.store.reserveIdempotency(
        request.idempotencyKey,
        RunStore.fingerprint(fingerprintPayload),
        runId,
      );
      if (reservation.replayed) {
        const existing = this.store.getRun(reservation.runId);
        if (existing) {
          return { result: existing, replayed: true };
        }
      }
    }

    // Each run gets a FRESH source registry: fixture call counters are per-run.
    const registry = getScenario(request.scenario).buildRegistry();
    const kernel = new ExecutionKernel(registry);
    const log = new RunLog(runId, [this.store.eventSink(), jsonlFileSink(this.config.logPath)]);
    log.event('REQUEST_ACCEPTED', 'compose request accepted', undefined, {
      contract: request.contract,
      scenario: request.scenario ?? 'healthy',
      idempotencyKey: request.idempotencyKey ?? null,
    });

    const result = await kernel.execute(contract, {
      input,
      timeoutMs,
      snapshotToken,
      runId,
      log,
    });
    this.store.saveRun(result);
    return { result, replayed: false };
  }

  private clampTimeout(requested: number | undefined): number {
    const timeout = requested ?? this.config.defaultTimeoutMs;
    if (typeof timeout !== 'number' || !Number.isFinite(timeout) || timeout <= 0) {
      throw inputError('INVALID_TIMEOUT', 'timeoutMs must be a positive number', { timeout });
    }
    if (timeout > this.config.maxTimeoutMs) {
      throw inputError(
        'TIMEOUT_OUT_OF_BOUNDS',
        `timeoutMs ${timeout} exceeds maximum ${this.config.maxTimeoutMs}`,
        { maxTimeoutMs: this.config.maxTimeoutMs },
      );
    }
    return timeout;
  }
}
