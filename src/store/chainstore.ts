import Database from "better-sqlite3";
import { AppError } from "../errors";
import { LIMITS } from "../config";

export type RevisionStatus = "pending" | "active" | "superseded" | "revoked";

export interface ChainBlockRow {
  height: number;
  blockHash: string;
  parentHash: string | null;
}

export interface RevisionRow {
  tokenId: string;
  version: number;
  cid: string;
  anchorHeight: number;
  anchorHash: string;
  status: RevisionStatus;
  evidence: string;
  createdAt: number;
}

export interface TokenRow {
  tokenId: string;
  currentCid: string | null;
  createdAt: number;
}

export interface CanonicalView {
  tipHeight: number;
  /** Confirmations for a revision anchored at height h: tip - h + 1. */
  confirmationsFor(anchorHeight: number): number;
  isCanonical(height: number): boolean;
}

export class ChainStore {
  constructor(private readonly db: Database.Database) {}

  // ---------------------------------------------------------------- chain --

  tip(): ChainBlockRow | null {
    return (
      (this.db
        .prepare(
          `SELECT height, block_hash AS blockHash, parent_hash AS parentHash
           FROM chain_blocks ORDER BY height DESC LIMIT 1`
        )
        .get() as ChainBlockRow | undefined) ?? null
    );
  }

  getByHeight(height: number): ChainBlockRow | null {
    return (
      (this.db
        .prepare(
          `SELECT height, block_hash AS blockHash, parent_hash AS parentHash
           FROM chain_blocks WHERE height = ?`
        )
        .get(height) as ChainBlockRow | undefined) ?? null
    );
  }

  listBlocks(): ChainBlockRow[] {
    return this.db
      .prepare(
        `SELECT height, block_hash AS blockHash, parent_hash AS parentHash
         FROM chain_blocks ORDER BY height`
      )
      .all() as ChainBlockRow[];
  }

  /** Append a new block at height = tip+1. The chain starts at height 0. */
  appendBlock(blockHash: string, parentHash: string | null): ChainBlockRow {
    const tip = this.tip();
    if (!tip) {
      if (parentHash !== null) {
        throw new AppError("BAD_REQUEST", "genesis block must have parentHash = null");
      }
      this.db
        .prepare(
          `INSERT INTO chain_blocks (height, block_hash, parent_hash) VALUES (?, ?, ?)`
        )
        .run(0, blockHash, null);
      this.recomputeAllStatuses();
      return this.getByHeight(0)!;
    }
    const height = tip.height + 1;
    if (parentHash !== tip.blockHash) {
      throw new AppError(
        "CHAIN_CONFLICT",
        `parentHash does not match the current tip at height ${tip.height}`,
        { expected: tip.blockHash, received: parentHash }
      );
    }
    this.db
      .prepare(
        `INSERT INTO chain_blocks (height, block_hash, parent_hash) VALUES (?, ?, ?)`
      )
      .run(height, blockHash, parentHash);
    this.recomputeAllStatuses();
    return this.getByHeight(height)!;
  }

  /**
   * Replace the canonical tip.
   *
   * @param fromHeight first slot that diverges; old blocks from here up are
   *                   abandoned
   * @param newBlocks  replacement blocks starting at fromHeight
   */
  reorganize(fromHeight: number, newBlocks: Array<{ hash: string; parent: string | null }>): void {
    const tip = this.tip();
    if (!tip) throw new AppError("BAD_REQUEST", "cannot reorganize an empty chain");
    if (fromHeight <= 0 || fromHeight > tip.height) {
      throw new AppError("BAD_REQUEST", "fromHeight out of range");
    }
    const removedCount = tip.height - fromHeight + 1;
    if (removedCount >= LIMITS.REQUIRED_CONFIRMATIONS) {
      throw new AppError(
        "DEEP_REORG_REJECTED",
        `reorg would abandon ${removedCount} blocks but finality is ` +
          `${LIMITS.REQUIRED_CONFIRMATIONS} confirmations; rejecting deep reorg`,
        { removedCount, finality: LIMITS.REQUIRED_CONFIRMATIONS }
      );
    }

    const parentAtFork = fromHeight === 0 ? null : this.getByHeight(fromHeight - 1)?.blockHash ?? null;
    const firstParent = newBlocks[0]?.parent ?? null;
    if (firstParent !== parentAtFork) {
      throw new AppError(
        "CHAIN_CONFLICT",
        "first replacement block's parent does not connect to the fork parent",
        { expected: parentAtFork, received: firstParent }
      );
    }
    for (let i = 1; i < newBlocks.length; i++) {
      if (newBlocks[i].parent !== newBlocks[i - 1].hash) {
        throw new AppError("CHAIN_CONFLICT", `replacement blocks not linked at index ${i}`);
      }
    }
    const expectedNewTip = fromHeight + newBlocks.length - 1;
    if (expectedNewTip < fromHeight) {
      throw new AppError("BAD_REQUEST", "reorg must provide at least one replacement block");
    }

    const tx = this.db.transaction(() => {
      this.db.prepare(`DELETE FROM chain_blocks WHERE height >= ?`).run(fromHeight);
      let h = fromHeight;
      for (const b of newBlocks) {
        this.db
          .prepare(
            `INSERT INTO chain_blocks (height, block_hash, parent_hash) VALUES (?, ?, ?)`
          )
          .run(h, b.hash, b.parent);
        h++;
      }
      // Revisions anchored on abandoned slots are revoked (unconfirmed updates).
      this.db
        .prepare(
          `UPDATE revisions SET status = 'revoked'
           WHERE anchor_height >= ? AND status != 'revoked'`
        )
        .run(fromHeight);
      this.recomputeAllStatusesTx();
    });
    tx();
  }

  // --------------------------------------------------------------- tokens --

  getToken(tokenId: string): TokenRow | null {
    return (
      (this.db
        .prepare(
          `SELECT token_id AS tokenId, current_cid AS currentCid, created_at AS createdAt
           FROM tokens WHERE token_id = ?`
        )
        .get(tokenId) as TokenRow | undefined) ?? null
    );
  }

  listTokens(): TokenRow[] {
    return this.db
      .prepare(
        `SELECT token_id AS tokenId, current_cid AS currentCid, created_at AS createdAt
         FROM tokens ORDER BY token_id`
      )
      .all() as TokenRow[];
  }

  listRevisions(tokenId: string): RevisionRow[] {
    return this.db
      .prepare(
        `SELECT token_id AS tokenId, version, cid,
                anchor_height AS anchorHeight, anchor_hash AS anchorHash,
                status, evidence, created_at AS createdAt
         FROM revisions WHERE token_id = ? ORDER BY version`
      )
      .all(tokenId) as RevisionRow[];
  }

  getRevision(tokenId: string, version: number): RevisionRow | null {
    return (
      (this.db
        .prepare(
          `SELECT token_id AS tokenId, version, cid,
                  anchor_height AS anchorHeight, anchor_hash AS anchorHash,
                  status, evidence, created_at AS createdAt
           FROM revisions WHERE token_id = ? AND version = ?`
        )
        .get(tokenId, version) as RevisionRow | undefined) ?? null
    );
  }

  highestVersion(tokenId: string): number | null {
    const row = this.db
      .prepare(
        `SELECT max(version) AS v FROM revisions
         WHERE token_id = ? AND status != 'revoked'`
      )
      .get(tokenId) as { v: number | null };
    return row.v ?? null;
  }

  /**
   * Register a new metadata revision.
   *
   * version rules: first revision must be 1; later ones must equal
   * highest-non-revoked + 1 (re-registering an old/duplicate version is
   * rejected even when later revisions got revoked by a reorg).
   */
  registerRevision(args: {
    tokenId: string;
    version: number;
    cid: string;
    anchorHeight: number;
    anchorHash: string;
    evidence: unknown;
  }): RevisionRow {
    const tip = this.tip();
    if (!tip) throw new AppError("BAD_REQUEST", "chain is empty; POST /chain/blocks first");
    if (args.anchorHeight !== tip.height || args.anchorHash !== tip.blockHash) {
      throw new AppError("CHAIN_CONFLICT", "anchor no longer matches the chain tip", {
        tipHeight: tip.height,
        tipHash: tip.blockHash,
      });
    }

    const tx = this.db.transaction(() => {
      const highest = this.highestVersion(args.tokenId);
      const expected = highest === null ? 1 : highest + 1;
      if (args.version !== expected) {
        throw new AppError(
          "VERSION_CONFLICT",
          highest === null
            ? `first revision must have version 1, got ${args.version}`
            : `expected next metadata version ${expected} for token '${args.tokenId}', ` +
              `got ${args.version} (duplicate/old versions are rejected)`,
          { expectedVersion: expected, receivedVersion: args.version }
        );
      }
      const now = Date.now();
      this.db
        .prepare(
          `INSERT INTO tokens (token_id, current_cid, created_at)
           VALUES (?, ?, ?)
           ON CONFLICT(token_id) DO UPDATE SET current_cid = excluded.current_cid`
        )
        .run(args.tokenId, args.cid, now);
      this.db
        .prepare(
          `INSERT INTO revisions
             (token_id, version, cid, anchor_height, anchor_hash, status, evidence, created_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)`
        )
        .run(
          args.tokenId,
          args.version,
          args.cid,
          args.anchorHeight,
          args.anchorHash,
          JSON.stringify(args.evidence),
          now
        );
      this.recomputeAllStatusesTx();
    });
    tx();
    return this.getRevision(args.tokenId, args.version)!;
  }

  /**
   * Recompute every revision status from the current canonical chain:
   *  - revoked rows stay revoked (anchored on an abandoned fork);
   *  - the highest non-revoked version is 'active' if it has reached the
   *    required confirmations, else 'pending';
   *  - all earlier non-revoked revisions are 'superseded'.
   * tokens.current_cid points at the highest non-revoked revision.
   */
  private recomputeAllStatusesTx(): void {
    const tip = this.db
      .prepare(`SELECT max(height) AS h FROM chain_blocks`)
      .get() as { h: number | null };
    const tipHeight = tip.h;

    const rows = this.db
      .prepare(`SELECT rowid AS rowid, token_id AS tokenId, version, anchor_height AS h FROM revisions`)
      .all() as Array<{ rowid: number; tokenId: string; version: number; h: number }>;

    const byToken = new Map<string, Array<{ rowid: number; version: number; h: number }>>();
    for (const r of rows) {
      if (!byToken.has(r.tokenId)) byToken.set(r.tokenId, []);
      byToken.get(r.tokenId)!.push(r);
    }

    const update = this.db.prepare(`UPDATE revisions SET status = ? WHERE rowid = ?`);
    const setCurrent = this.db.prepare(`UPDATE tokens SET current_cid = ? WHERE token_id = ?`);

    for (const [tokenId, list] of byToken) {
      const live = list.filter((r) => {
        const status = (
          this.db.prepare(`SELECT status FROM revisions WHERE rowid = ?`).get(r.rowid) as {
            status: RevisionStatus;
          }
        ).status;
        return status !== "revoked";
      });
      if (live.length === 0) {
        setCurrent.run(null, tokenId);
        continue;
      }
      live.sort((a, b) => b.version - a.version);
      const latest = live[0];
      for (let i = 0; i < live.length; i++) {
        const r = live[i];
        let status: RevisionStatus;
        if (i > 0) status = "superseded";
        else if (tipHeight !== null && tipHeight - r.h + 1 >= LIMITS.REQUIRED_CONFIRMATIONS) {
          status = "active";
        } else {
          status = "pending";
        }
        update.run(status, r.rowid);
      }
      setCurrent.run(
        (
          this.db
            .prepare(`SELECT cid FROM revisions WHERE rowid = ?`)
            .get(latest.rowid) as { cid: string }
        ).cid,
        tokenId
      );
    }
  }

  recomputeAllStatuses(): void {
    const tx = this.db.transaction(() => this.recomputeAllStatusesTx());
    tx();
  }

  /** Current confirmation count for a revision row. */
  confirmations(anchorHeight: number): number {
    const tip = this.tip();
    if (!tip || anchorHeight > tip.height) return 0;
    return tip.height - anchorHeight + 1;
  }
}
