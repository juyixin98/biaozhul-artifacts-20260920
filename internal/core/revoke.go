package core

import (
	"context"
	"fmt"

	"inbox/internal/crypto"
)

// SubmitRevocation applies validator-signed evidence that the current
// unconfirmed canonical tip of a chain is void (a pre-finality reorg).
//
// Rules:
//   - The evidence must be signed by the chain's registered validator.
//   - The referenced block must exist, be canonical/active, and be the
//     current tip (only the single unconfirmed tip can be revoked; deeper
//     reorgs require revoking tip-by-tip).
//   - The tip must not be finalized.
//   - On success the tip is marked revoked/non-canonical, and every still
//     un-executed candidate message bound to it is cancelled. Already
//     confirmed/executed messages cannot exist under a non-final tip, so
//     executed history is never touched.
func (e *Executor) SubmitRevocation(ctx context.Context, r Revocation) error {
	evidence := crypto.RevocationHash(r.ChainID, r.Block)
	if err := crypto.Verify(r.Validator, evidence, r.Signature); err != nil {
		return domErr(CodeBadSignature, "revocation evidence: %v", err)
	}

	err := e.inTx(ctx, func(tx q) error {
		chain, err := loadChainQ(ctx, tx, r.ChainID)
		if err != nil {
			return err
		}
		if !bytesEqual(chain.validatorPub, r.Validator) {
			return domErr(CodeBadSignature, "signer is not the registered validator for chain %q", r.ChainID)
		}
		if err := advisoryLock(ctx, tx, chainAdvKey(r.ChainID)); err != nil {
			return err
		}

		var height uint64
		var status string
		var canonical bool
		err = tx.QueryRow(ctx,
			`SELECT height, status, canonical FROM headers WHERE chain_id = $1 AND block_hash = $2`,
			r.ChainID, r.Block[:]).Scan(&height, &status, &canonical)
		if isNoRows(err) {
			return domErr(CodeBlockUnknown, "block %x is not known on chain %q", r.Block[:], r.ChainID)
		}
		if err != nil {
			return err
		}
		if status != "active" || !canonical {
			return domErr(CodeTipNotCurrent, "block %x is not an active canonical header", r.Block[:])
		}

		tipHeight, _, _, err := canonicalTip(ctx, tx, r.ChainID)
		if err != nil {
			return err
		}
		if isFinal(tipHeight, height, chain.confirmations) {
			return domErr(CodeBlockFinalized,
				"cannot revoke block %x at height %d: already finalized under tip %d",
				r.Block[:], height, tipHeight)
		}
		if height != tipHeight {
			return domErr(CodeTipNotCurrent,
				"only the current tip (height %d) can be revoked; block is at height %d (revoke tip-by-tip)",
				tipHeight, height)
		}

		if _, err := tx.Exec(ctx,
			`UPDATE headers SET status = 'revoked', canonical = false
			 WHERE chain_id = $1 AND block_hash = $2`,
			r.ChainID, r.Block[:]); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx,
			`INSERT INTO revoked_blocks (chain_id, height, block_hash, evidence_sig)
			 VALUES ($1,$2,$3,$4)`,
			r.ChainID, height, r.Block[:], r.Signature); err != nil {
			return err
		}

		// Cancel candidate (un-executed) messages bound to the revoked tip.
		tag, err := tx.Exec(ctx,
			`UPDATE messages SET status = 'cancelled', updated_at = now()
			 WHERE chain_id = $1 AND block_hash = $2 AND status = 'candidate'`,
			r.ChainID, r.Block[:])
		if err != nil {
			return err
		}
		cancelled := int(tag.RowsAffected())

		if err := raiseAlert(ctx, tx, "warning", "tip_revoked", r.ChainID, "",
			fmt.Sprintf("unconfirmed tip at height %d revoked via validator evidence; %d candidate message(s) cancelled",
				height, cancelled)); err != nil {
			return err
		}
		return nil
	})
	if err != nil {
		return err
	}
	e.notify()
	return nil
}
