import Database from 'better-sqlite3';
import { randomUUID } from 'node:crypto';
import type { DiffResult, Finding } from '../kernel/types.js';

/**
 * SQLite-backed state adapter.
 *
 * Stores contract versions, diff analyses, per-finding rows and structured
 * processing logs keyed by a request id, so a failure can be reconstructed:
 * request identity -> steps/versions -> findings -> uncertainty notes.
 */

export interface StoredContract {
  id: number;
  contract_ref: string;
  version_label: string;
  title: string;
  source_text: string;
  created_at: string;
}

export interface StoredAnalysis {
  id: string;
  request_id: string;
  old_contract_id: number;
  new_contract_id: number;
  compatible: number;
  created_at: string;
}

export interface AnalysisRow extends StoredAnalysis {
  result: DiffResult;
}

export interface LogRow {
  seq: number;
  request_id: string;
  analysis_id: string | null;
  level: 'debug' | 'info' | 'warn' | 'error';
  step: string;
  message: string;
  context: string; // JSON
  created_at: string;
}

export class ContractStore {
  private readonly db: Database.Database;

  constructor(path = ':memory:') {
    this.db = new Database(path);
    this.db.pragma('journal_mode = WAL');
    this.db.pragma('foreign_keys = ON');
    this.migrate();
  }

  close(): void {
    this.db.close();
  }

  private migrate(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS contracts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contract_ref TEXT NOT NULL,
        version_label TEXT NOT NULL,
        title TEXT NOT NULL,
        source_text TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(contract_ref, version_label)
      );
      CREATE TABLE IF NOT EXISTS analyses (
        id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL,
        old_contract_id INTEGER NOT NULL REFERENCES contracts(id),
        new_contract_id INTEGER NOT NULL REFERENCES contracts(id),
        compatible INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      );
      CREATE TABLE IF NOT EXISTS findings (
        id TEXT NOT NULL,
        analysis_id TEXT NOT NULL REFERENCES analyses(id),
        direction TEXT NOT NULL,
        category TEXT NOT NULL,
        severity TEXT NOT NULL,
        operation TEXT NOT NULL,
        location TEXT NOT NULL,
        message TEXT NOT NULL,
        old_side TEXT NOT NULL,
        new_side TEXT NOT NULL,
        witness_json TEXT NOT NULL,
        uncertainty TEXT
      );
      CREATE TABLE IF NOT EXISTS processing_logs (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL,
        analysis_id TEXT,
        level TEXT NOT NULL,
        step TEXT NOT NULL,
        message TEXT NOT NULL,
        context TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      );
      CREATE INDEX IF NOT EXISTS idx_analyses_request ON analyses(request_id);
      CREATE INDEX IF NOT EXISTS idx_findings_analysis ON findings(analysis_id);
      CREATE INDEX IF NOT EXISTS idx_logs_request ON processing_logs(request_id);
    `);
  }

  upsertContract(ref: string, versionLabel: string, title: string, sourceText: string): StoredContract {
    const existing = this.db
      .prepare('SELECT * FROM contracts WHERE contract_ref = ? AND version_label = ?')
      .get(ref, versionLabel) as StoredContract | undefined;
    if (existing) return existing;
    const info = this.db
      .prepare(
        'INSERT INTO contracts (contract_ref, version_label, title, source_text) VALUES (?, ?, ?, ?)',
      )
      .run(ref, versionLabel, title, sourceText);
    return this.db.prepare('SELECT * FROM contracts WHERE id = ?').get(info.lastInsertRowid) as StoredContract;
  }

  getContract(id: number): StoredContract | undefined {
    return this.db.prepare('SELECT * FROM contracts WHERE id = ?').get(id) as StoredContract | undefined;
  }

  log(requestId: string, level: LogRow['level'], step: string, message: string, context: unknown = {}, analysisId: string | null = null): void {
    this.db
      .prepare(
        'INSERT INTO processing_logs (request_id, analysis_id, level, step, message, context) VALUES (?, ?, ?, ?, ?, ?)',
      )
      .run(requestId, analysisId, level, step, message, JSON.stringify(context));
  }

  logsFor(requestId: string): LogRow[] {
    return this.db
      .prepare('SELECT * FROM processing_logs WHERE request_id = ? ORDER BY seq')
      .all(requestId) as LogRow[];
  }

  saveAnalysis(
    requestId: string,
    oldContractId: number,
    newContractId: number,
    result: DiffResult,
  ): StoredAnalysis {
    const id = randomUUID();
    const insertAnalysis = this.db.prepare(
      `INSERT INTO analyses (id, request_id, old_contract_id, new_contract_id, compatible)
       VALUES (?, ?, ?, ?, ?)`,
    );
    const insertFinding = this.db.prepare(
      `INSERT INTO findings (id, analysis_id, direction, category, severity, operation, location, message, old_side, new_side, witness_json, uncertainty)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    );
    const save = this.db.transaction(() => {
      insertAnalysis.run(id, requestId, oldContractId, newContractId, result.compatible ? 1 : 0);
      for (const f of [...result.requestFindings, ...result.responseFindings]) {
        insertFinding.run(
          f.id, id, f.direction, f.category, f.severity, f.operation, f.location,
          f.message, f.oldSide, f.newSide, JSON.stringify(f.witness), f.uncertainty ?? null,
        );
      }
    });
    save();
    return this.db.prepare('SELECT * FROM analyses WHERE id = ?').get(id) as StoredAnalysis;
  }

  getAnalysis(id: string): AnalysisRow | undefined {
    const analysis = this.db.prepare('SELECT * FROM analyses WHERE id = ?').get(id) as StoredAnalysis | undefined;
    if (!analysis) return undefined;
    const findingRows = this.db
      .prepare('SELECT * FROM findings WHERE analysis_id = ?')
      .all(id) as Array<Record<string, unknown>>;
    const requestFindings: Finding[] = [];
    const responseFindings: Finding[] = [];
    for (const row of findingRows) {
      const finding: Finding = {
        id: row['id'] as string,
        direction: row['direction'] as Finding['direction'],
        category: row['category'] as Finding['category'],
        severity: row['severity'] as Finding['severity'],
        operation: row['operation'] as string,
        location: row['location'] as string,
        message: row['message'] as string,
        oldSide: row['old_side'] as string,
        newSide: row['new_side'] as string,
        witness: JSON.parse(row['witness_json'] as string) as Finding['witness'],
        uncertainty: (row['uncertainty'] as string | null) ?? undefined,
      };
      (finding.direction === 'response' ? responseFindings : requestFindings).push(finding);
    }
    return {
      ...analysis,
      compatible: analysis.compatible,
      result: {
        requestFindings,
        responseFindings,
        extensionNotes: [],
        compatible: analysis.compatible === 1,
      },
    };
  }

  listAnalyses(requestId?: string): StoredAnalysis[] {
    if (requestId) {
      return this.db
        .prepare('SELECT * FROM analyses WHERE request_id = ? ORDER BY created_at DESC')
        .all(requestId) as StoredAnalysis[];
    }
    return this.db.prepare('SELECT * FROM analyses ORDER BY created_at DESC').all() as StoredAnalysis[];
  }
}
