/**
 * Test harness shared by kernel/transport tests. It only constructs the real
 * modules with in-memory state; expected values are never produced by the
 * code under test.
 */

import { SqliteLedgerStore } from "../../src/state/store.js";
import { Kernel } from "../../src/kernel/engine.js";
import type {
  DiagnosticsLogger,
  LogEvent,
} from "../../src/diagnostics/logger.js";
import { noopLogger } from "../../src/diagnostics/logger.js";

export class CapturingLogger implements DiagnosticsLogger {
  readonly events: LogEvent[] = [];
  private push(level: string, event: LogEvent): void {
    this.events.push({ ...event, decision: `[${level}] ${event.decision}` });
  }
  accepted(event: LogEvent): void {
    this.push("accepted", event);
  }
  rejected(event: LogEvent): void {
    this.push("rejected", event);
  }
  undecidable(event: LogEvent): void {
    this.push("undecidable", event);
  }
  warn(event: LogEvent): void {
    this.push("warn", event);
  }
  decisions(): string[] {
    return this.events.map((e) => e.decision);
  }
}

export interface Harness {
  readonly store: SqliteLedgerStore;
  readonly kernel: Kernel;
  readonly logger: CapturingLogger;
}

export function makeHarness(clock?: () => Date): Harness {
  const store = new SqliteLedgerStore(":memory:");
  const logger = new CapturingLogger();
  const kernel = new Kernel({ store, logger, clock });
  return { store, kernel, logger };
}

export { noopLogger };
