/**
 * State-adapter layer — SQLite implementation of the ResourceStore port.
 *
 * Concurrency model:
 * - Every write path runs in a single `BEGIN IMMEDIATE` transaction, so the
 *   read of the current snapshot, precondition evaluation and the write take
 *   the write lock together. A second concurrent writer blocks at BEGIN and
 *   re-evaluates against the first writer's committed state once it proceeds
 *   — no lost updates possible.
 * - busy_timeout makes lock contention a wait, not an immediate error;
 *   SQLITE_BUSY after the timeout is surfaced as "write-lock-busy".
 */

import Database from "better-sqlite3";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import type {
  DecisionRecord,
  EvalStep,
  HttpMethod,
  ConditionInput,
} from "../contract/model.js";
import {
  StorageError,
  type IdempotentOutcome,
  type NewVersionInput,
  type ResourceStore,
  type StoredResource,
  type StoreTransaction,
} from "./store.js";

interface ResourceRow {
  resource_id: string;
  version: number;
  body: string;
  etag: string;
  content_type: string;
  updated_at_ms: number;
}

interface HistoryRow {
  version: number;
  etag: string;
  updated_at_ms: number;
}

interface IdempotencyRow {
  resource_id: string;
  method: string;
  request_body_hash: string;
  status: number;
  response_body: string | null;
  response_etag: string | null;
  content_type: string | null;
  resulting_version: number | null;
  at_ms: number;
}

interface DecisionRow {
  run_id: string;
  client_id: string;
  request_id: string;
  method: HttpMethod;
  resource_id: string;
  request_body_hash: string | null;
  conditions_json: string;
  observed_version: number | null;
  observed_etag: string | null;
  observed_updated_at_ms: number | null;
  steps_json: string;
  verdict: string;
  failure_category: string | null;
  outcome_status: number;
  resulting_version: number | null;
  resulting_etag: string | null;
  replayed: number;
  at_ms: number;
}

function toStoredResource(row: ResourceRow): StoredResource {
  return {
    resourceId: row.resource_id,
    version: row.version,
    body: row.body,
    etag: row.etag,
    contentType: row.content_type,
    updatedAtMs: row.updated_at_ms,
  };
}

function toIdempotentOutcome(key: string, row: IdempotencyRow): IdempotentOutcome {
  void key;
  return {
    resourceId: row.resource_id,
    method: row.method,
    requestBodyHash: row.request_body_hash,
    status: row.status,
    responseBody: row.response_body,
    responseEtag: row.response_etag,
    contentType: row.content_type,
    resultingVersion: row.resulting_version,
    atMs: row.at_ms,
  };
}

export interface SqliteStoreOptions {
  readonly path: string;
  readonly busyTimeoutMs: number;
}

export class SqliteResourceStore implements ResourceStore {
  readonly kind = "sqlite";
  private readonly db: Database.Database;

  constructor(options: SqliteStoreOptions) {
    if (options.path !== ":memory:") {
      const dir = dirname(options.path);
      if (dir !== ".") mkdirSync(dir, { recursive: true });
    }
    this.db = new Database(options.path);
    this.db.pragma("journal_mode = WAL");
    this.db.pragma("foreign_keys = ON");
    this.db.pragma("busy_timeout = " + Math.trunc(options.busyTimeoutMs));
    this.db.pragma("synchronous = FULL");
    this.migrate();
  }

  private migrate(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS resources (
        resource_id    TEXT PRIMARY KEY,
        version        INTEGER NOT NULL,
        body           TEXT NOT NULL,
        etag           TEXT NOT NULL,
        content_type   TEXT NOT NULL,
        updated_at_ms  INTEGER NOT NULL
      );

      CREATE TABLE IF NOT EXISTS resource_history (
        resource_id    TEXT NOT NULL,
        version        INTEGER NOT NULL,
        body           TEXT NOT NULL,
        etag           TEXT NOT NULL,
        content_type   TEXT NOT NULL,
        updated_at_ms  INTEGER NOT NULL,
        PRIMARY KEY (resource_id, version)
      );

      CREATE TABLE IF NOT EXISTS idempotency (
        idempotency_key TEXT PRIMARY KEY,
        resource_id     TEXT NOT NULL,
        method          TEXT NOT NULL,
        request_body_hash TEXT NOT NULL,
        status          INTEGER NOT NULL,
        response_body   TEXT,
        response_etag  TEXT,
        content_type    TEXT,
        resulting_version INTEGER,
        at_ms           INTEGER NOT NULL
      );

      CREATE TABLE IF NOT EXISTS decisions (
        seq             INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id          TEXT NOT NULL,
        client_id       TEXT NOT NULL,
        request_id      TEXT NOT NULL,
        method          TEXT NOT NULL,
        resource_id     TEXT NOT NULL,
        request_body_hash TEXT,
        conditions_json TEXT NOT NULL,
        observed_version INTEGER,
        observed_etag   TEXT,
        observed_updated_at_ms INTEGER,
        steps_json      TEXT NOT NULL,
        verdict         TEXT NOT NULL,
        failure_category TEXT,
        outcome_status  INTEGER NOT NULL,
        resulting_version INTEGER,
        resulting_etag  TEXT,
        replayed        INTEGER NOT NULL,
        at_ms           INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions(run_id, seq);
      CREATE INDEX IF NOT EXISTS idx_decisions_resource ON decisions(resource_id, seq);
    `);
  }

  run<T>(mode: "deferred" | "immediate", fn: (tx: StoreTransaction) => T): T {
    // better-sqlite3 binds one function to one transaction; the .immediate()
    // / .deferred() binders choose BEGIN IMMEDIATE vs BEGIN. On throw it
    // issues ROLLBACK automatically.
    const bound = this.db.transaction((): T => fn(this.createTransaction()));
    try {
      return mode === "immediate" ? bound.immediate() : bound.deferred();
    } catch (err) {
      throw mapSqliteError(err);
    }
  }

  readCommitted<T>(fn: (tx: StoreTransaction) => T): T {
    // No explicit transaction: better-sqlite3 issues the reads in autocommit
    // mode, after the writer has committed. This is the snapshot actually
    // returned to the client.
    return fn(this.createTransaction());
  }

  listDecisions(filter: {
    runId?: string;
    clientId?: string;
    resourceId?: string;
  }): DecisionRecord[] {
    const clauses: string[] = [];
    const params: Record<string, string> = {};
    if (filter.runId) {
      clauses.push("run_id = @runId");
      params.runId = filter.runId;
    }
    if (filter.clientId) {
      clauses.push("client_id = @clientId");
      params.clientId = filter.clientId;
    }
    if (filter.resourceId) {
      clauses.push("resource_id = @resourceId");
      params.resourceId = filter.resourceId;
    }
    const where = clauses.length > 0 ? "WHERE " + clauses.join(" AND ") : "";
    const rows = this.db
      .prepare(`SELECT * FROM decisions ${where} ORDER BY seq ASC`)
      .all(params) as DecisionRow[];
    return rows.map(decisionFromRow);
  }

  listHistory(resourceId: string): Array<{ version: number; etag: string; updatedAtMs: number }> {
    const rows = this.db
      .prepare(
        "SELECT version, etag, updated_at_ms FROM resource_history WHERE resource_id = ? ORDER BY version ASC",
      )
      .all(resourceId) as HistoryRow[];
    return rows.map((r) => ({ version: r.version, etag: r.etag, updatedAtMs: r.updated_at_ms }));
  }

  close(): void {
    this.db.close();
  }

  private createTransaction(): StoreTransaction {
    const db = this.db;
    return {
      getCurrent(resourceId) {
        const row = db
          .prepare("SELECT * FROM resources WHERE resource_id = ?")
          .get(resourceId) as ResourceRow | undefined;
        return row ? toStoredResource(row) : null;
      },
      nextVersionFromHistory(resourceId) {
        const row = db
          .prepare(
            "SELECT COALESCE(MAX(version), 0) AS max_version FROM resource_history WHERE resource_id = ?",
          )
          .get(resourceId) as { max_version: number };
        return row.max_version + 1;
      },
      upsertCurrent(input: NewVersionInput) {
        db.prepare(
          `INSERT INTO resources (resource_id, version, body, etag, content_type, updated_at_ms)
           VALUES (@resourceId, @version, @body, @etag, @contentType, @updatedAtMs)
           ON CONFLICT(resource_id) DO UPDATE SET
             version = excluded.version,
             body = excluded.body,
             etag = excluded.etag,
             content_type = excluded.content_type,
             updated_at_ms = excluded.updated_at_ms`,
        ).run(toNewVersionParams(input));
      },
      insertHistory(input: NewVersionInput) {
        db.prepare(
          `INSERT INTO resource_history (resource_id, version, body, etag, content_type, updated_at_ms)
           VALUES (@resourceId, @version, @body, @etag, @contentType, @updatedAtMs)`,
        ).run(toNewVersionParams(input));
      },
      deleteCurrent(resourceId) {
        db.prepare("DELETE FROM resources WHERE resource_id = ?").run(resourceId);
      },
      getIdempotentOutcome(key) {
        const row = db
          .prepare("SELECT * FROM idempotency WHERE idempotency_key = ?")
          .get(key) as IdempotencyRow | undefined;
        return row ? toIdempotentOutcome(key, row) : null;
      },
      putIdempotentOutcome(key, outcome) {
        db.prepare(
          `INSERT INTO idempotency (
             idempotency_key, resource_id, method, request_body_hash, status,
             response_body, response_etag, content_type, resulting_version, at_ms
           ) VALUES (
             @key, @resourceId, @method, @requestBodyHash, @status,
             @responseBody, @responseEtag, @contentType, @resultingVersion, @atMs
           )
           ON CONFLICT(idempotency_key) DO NOTHING`,
        ).run({
          key,
          resourceId: outcome.resourceId,
          method: outcome.method,
          requestBodyHash: outcome.requestBodyHash,
          status: outcome.status,
          responseBody: outcome.responseBody,
          responseEtag: outcome.responseEtag,
          contentType: outcome.contentType,
          resultingVersion: outcome.resultingVersion,
          atMs: outcome.atMs,
        });
      },
      insertDecision(record) {
        db.prepare(
          `INSERT INTO decisions (
             run_id, client_id, request_id, method, resource_id, request_body_hash,
             conditions_json, observed_version, observed_etag, observed_updated_at_ms,
             steps_json, verdict, failure_category, outcome_status,
             resulting_version, resulting_etag, replayed, at_ms
           ) VALUES (
             @runId, @clientId, @requestId, @method, @resourceId, @requestBodyHash,
             @conditionsJson, @observedVersion, @observedEtag, @observedUpdatedAtMs,
             @stepsJson, @verdict, @failureCategory, @outcomeStatus,
             @resultingVersion, @resultingEtag, @replayed, @atMs
           )`,
        ).run(decisionToParams(record));
      },
    };
  }
}

function toNewVersionParams(input: NewVersionInput) {
  return {
    resourceId: input.resourceId,
    version: input.version,
    body: input.body,
    etag: input.etag,
    contentType: input.contentType,
    updatedAtMs: input.updatedAtMs,
  };
}

function decisionToParams(r: DecisionRecord): Record<string, unknown> {
  // better-sqlite3 binds @name to the camelCase object key.
  return {
    runId: r.runId,
    clientId: r.clientId,
    requestId: r.requestId,
    method: r.method,
    resourceId: r.resourceId,
    requestBodyHash: r.requestBodyHash,
    conditionsJson: JSON.stringify(r.conditions),
    observedVersion: r.observedVersion,
    observedEtag: r.observedEtag,
    observedUpdatedAtMs: r.observedUpdatedAtMs,
    stepsJson: JSON.stringify(r.steps),
    verdict: r.verdict,
    failureCategory: r.failureCategory,
    outcomeStatus: r.outcomeStatus,
    resultingVersion: r.resultingVersion,
    resultingEtag: r.resultingEtag,
    replayed: r.replayed ? 1 : 0,
    atMs: r.atMs,
  };
}

function decisionFromRow(row: DecisionRow): DecisionRecord {
  return {
    runId: row.run_id,
    clientId: row.client_id,
    requestId: row.request_id,
    method: row.method,
    resourceId: row.resource_id,
    requestBodyHash: row.request_body_hash,
    conditions: JSON.parse(row.conditions_json) as ConditionInput,
    observedVersion: row.observed_version,
    observedEtag: row.observed_etag,
    observedUpdatedAtMs: row.observed_updated_at_ms,
    steps: JSON.parse(row.steps_json) as EvalStep[],
    verdict: row.verdict as DecisionRecord["verdict"],
    failureCategory: row.failure_category as DecisionRecord["failureCategory"],
    outcomeStatus: row.outcome_status,
    resultingVersion: row.resulting_version,
    resultingEtag: row.resulting_etag,
    replayed: row.replayed === 1,
    atMs: row.at_ms,
  };
}

export function mapSqliteError(err: unknown): unknown {
  if (
    typeof err === "object" &&
    err !== null &&
    "code" in err &&
    (err as { code?: string }).code === "SQLITE_BUSY"
  ) {
    return new StorageError("Database write lock busy (SQLITE_BUSY)", "write-lock-busy");
  }
  return err;
}
