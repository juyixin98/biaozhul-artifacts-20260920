/**
 * Metadata versioning + simulated chain confirmation/reorg logic.
 *
 * Lifecycle of a metadata version for one token:
 *
 *   proposed (at height H, block hash BH)
 *     └─ tip reaches H + confirmations-1 ─► confirmed/effective; the previous
 *                                           confirmed version -> historical
 *     └─ reorg event at height ≤ H with  ─► reorged (the unconfirmed update is
 *        a replaced block                      undone; the old effective CID
 *                                             remains in force)
 *
 * A confirmed version is never auto-reverted: if a reported reorg would
 * unwind an already-effective proposal, the service reports it as protected
 * instead of silently rolling back.
 */
import type { Store, MetadataVersionRow } from './store.js';

export interface Tip {
  height: number;
  blockHash: string;
}

export class ChainService {
  constructor(
    private readonly store: Store,
    private readonly confirmations: number,
  ) {}

  getTip(): Tip | undefined {
    const t = this.store.getTip();
    return t ? { height: t.height, blockHash: t.block_hash } : undefined;
  }

  /**
   * Move the chain tip forward (height must strictly increase).
   * Proposals with enough confirmations become effective in the same call.
   */
  advanceTip(height: number, blockHash: string): {
    tip: Tip;
    confirmed: MetadataVersionRow[];
  } {
    const prev = this.store.getTip();
    if (prev) {
      if (height <= prev.height) {
        const e = new Error(`cannot move tip backwards (${height} <= ${prev.height}); use /chain/reorg`);
        (e as NodeJS.ErrnoException).code = 'E_CONFLICT';
        throw e;
      }
      if (blockHash === prev.block_hash) {
        throw new Error('new tip block hash must differ from the previous tip hash');
      }
    }
    assertHash(blockHash, 'blockHash');
    this.store.setTip(height, blockHash);

    // A proposal at height H has `confirmations` blocks on top once the tip
    // reaches H + confirmations; it becomes effective then.
    const cutoff = height - this.confirmations;
    const confirmed: MetadataVersionRow[] = [];
    for (const tokenId of this.store.tokensWithProposals()) {
      const proposal = this.store.hasPendingProposal(tokenId);
      if (proposal && proposal.proposed_height <= cutoff) this.confirm(proposal, height, blockHash, confirmed);
    }

    this.store.logEvent({
      type: 'CONFIRM',
      token_id: '*',
      version: null,
      cid: null,
      height,
      block_hash: blockHash,
      detail: confirmed.length ? `tip ${height}: ${confirmed.length} proposal(s) became effective` : `tip advanced to ${height}`,
    });
    return { tip: { height, blockHash }, confirmed };
  }

  private confirm(
    proposal: MetadataVersionRow,
    tipHeight: number,
    tipHash: string,
    out: MetadataVersionRow[],
  ): void {
    // The previous effective version is superseded atomically with promotion.
    this.store.supersedeConfirmedFor(proposal.token_id);
    this.store.confirmVersion(proposal.id, tipHeight, tipHash);
    this.store.logEvent({
      type: 'CONFIRM',
      token_id: proposal.token_id,
      version: proposal.version,
      cid: proposal.cid,
      height: tipHeight,
      block_hash: tipHash,
      detail: `metadata v${proposal.version} (${proposal.cid.slice(0, 18)}…) became effective at block ${tipHash}`,
    });
    out.push(proposal);
  }

  /**
   * Report a chain reorganization: the block at `height` (hash oldHash) was
   * replaced by newHash. Every *unconfirmed* proposal proposed at that height
   * or later is revoked. Proposals already confirmed in the affected window
   * are returned as protected — callers must explicitly resolve those.
   */
  applyReorg(height: number, oldHash: string, newHash: string): {
    reverted: MetadataVersionRow[];
    protected: MetadataVersionRow[];
  } {
    if (oldHash !== '*') assertHash(oldHash, 'oldHash');
    assertHash(newHash, 'newHash');
    if (oldHash === newHash) throw new Error('reorg requires distinct block hashes');

    const reverted: MetadataVersionRow[] = [];
    const protectedRows: MetadataVersionRow[] = [];
    for (const row of this.store.versionsFromHeight(height)) {
      const hashMatches = oldHash === '*' || row.proposed_block_hash.toLowerCase() === oldHash.toLowerCase();
      if (!hashMatches) continue;
      if (row.status === 'proposed') {
        this.store.reorgProposal(row.id);
        this.store.logEvent({
          type: 'REORG',
          token_id: row.token_id,
          version: row.version,
          cid: row.cid,
          height,
          block_hash: newHash,
          detail: `unconfirmed v${row.version} proposed at h${row.proposed_height} was revoked; effective metadata unchanged`,
        });
        reverted.push(row);
      } else if (row.status === 'confirmed') {
        protectedRows.push(row);
      }
    }

    this.store.logEvent({
      type: 'REORG',
      token_id: '*',
      version: null,
      cid: null,
      height,
      block_hash: newHash,
      detail: `reorg at h${height}: ${reverted.length} unconfirmed update(s) undone, ${protectedRows.length} confirmed protected`,
    });
    return { reverted, protected: protectedRows };
  }

  currentOf(tokenId: string): MetadataVersionRow | undefined {
    return this.store.currentVersion(tokenId);
  }
}

function assertHash(hash: string, field: string): void {
  if (!/^0x[0-9a-fA-F]{8,64}$/.test(hash)) {
    throw new Error(`${field} must be "0x" followed by 4-32 hex digits`);
  }
}
