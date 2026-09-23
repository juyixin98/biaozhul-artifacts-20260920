package core

import (
	"context"
	"crypto/ed25519"
	"fmt"

	"inbox/internal/crypto"
)

// SubmitHeader applies a signed source block header.
//
// Rules:
//   - The header must be signed by the chain's registered validator.
//   - Genesis (height 1) must have a zero parent; subsequent headers must
//     extend the canonical chain (parent must be the canonical active tip).
//   - A re-submission of the identical canonical header is an idempotent
//     no-op.
//   - A different header at a height occupied by an unconfirmed tip is
//     rejected: the relayer must first publish validator-signed revocation
//     evidence (SubmitRevocation). Nothing is silently replaced.
//   - A different header at a finalized height is an equivocation: channels
//     carrying already-executed messages from that block (and its
//     descendants) are frozen, critical alerts are raised, and the
//     conflicting header is rejected. Executed history is never rewritten.
func (e *Executor) SubmitHeader(ctx context.Context, h Header, validator ed25519.PublicKey, sig []byte) error {
	// Authenticate the header before touching any state.
	blockHash := crypto.HeaderHash(h.ChainID, h.Height, h.Parent, h.MsgRoot, h.Timestamp)
	if h.Block != (crypto.Hash{}) && h.Block != blockHash {
		return domErr(CodeBadRequest, "block hash does not match header fields")
	}
	h.Block = blockHash
	if err := crypto.Verify(validator, blockHash, sig); err != nil {
		return domErr(CodeBadSignature, "header signature: %v", err)
	}

	var equivocationErr error
	err := e.inTx(ctx, func(tx q) error {
		chain, err := loadChainQ(ctx, tx, h.ChainID)
		if err != nil {
			return err
		}
		if !bytesEqual(chain.validatorPub, validator) {
			return domErr(CodeBadSignature, "signer is not the registered validator for chain %q", h.ChainID)
		}

		// Chain-wide advisory lock serializes concurrent header/reorg work.
		if err := advisoryLock(ctx, tx, chainAdvKey(h.ChainID)); err != nil {
			return err
		}

		// Idempotent: identical header already known.
		var existingStatus string
		var existingCanonical bool
		err = tx.QueryRow(ctx,
			`SELECT status, canonical FROM headers WHERE chain_id = $1 AND block_hash = $2`,
			h.ChainID, h.Block[:]).Scan(&existingStatus, &existingCanonical)
		if err == nil {
			if existingStatus == "active" && existingCanonical {
				return nil
			}
			return domErr(CodeDuplicateHeader, "block hash already known in state %q", existingStatus)
		}
		if !isNoRows(err) {
			return err
		}

		tipHeight, tipHash, hasTip, err := canonicalTip(ctx, tx, h.ChainID)
		if err != nil {
			return err
		}

		// Occupant at this height?
		var occupantHash []byte
		err = tx.QueryRow(ctx,
			`SELECT block_hash FROM headers
			 WHERE chain_id = $1 AND height = $2 AND canonical AND status = 'active'`,
			h.ChainID, h.Height).Scan(&occupantHash)
		switch {
		case err == nil:
			if bytesEqual(occupantHash, h.Block[:]) {
				return nil
			}
			// A block at height H is final when tipHeight-H >= conf.
			if isFinal(tipHeight, h.Height, chain.confirmations) {
				// Incident side effects (alerts + freezes) are written
				// and COMMITTED in this transaction; the rejection error
				// is surfaced to the caller afterwards.
				equivocationErr = e.handleEquivocation(ctx, tx, chain, h, occupantHash)
				return nil
			}
			return domErr(CodeRevokeFirst,
				"height %d already has unconfirmed canonical tip %x; revoke it with validator evidence first",
				h.Height, occupantHash)
		case !isNoRows(err):
			return err
		}

		// No occupant: must extend the chain linearly.
		if hasTip {
			if h.Height != uint64(tipHeight)+1 {
				return domErr(CodeHeaderGap, "expected height %d after tip, got %d", tipHeight+1, h.Height)
			}
			if !bytesEqual(tipHash, h.Parent[:]) {
				return domErr(CodeBadParent, "parent hash does not match current canonical tip")
			}
		} else {
			if h.Height != 1 {
				return domErr(CodeHeaderGap, "first header must be height 1 (genesis), got %d", h.Height)
			}
			if h.Parent != (crypto.Hash{}) {
				return domErr(CodeBadParent, "genesis parent must be zero hash")
			}
		}

		if _, err := tx.Exec(ctx,
			`INSERT INTO headers (chain_id, height, block_hash, parent_hash, msg_root, block_time, status, canonical)
			 VALUES ($1,$2,$3,$4,$5,$6,'active',true)`,
			h.ChainID, h.Height, h.Block[:], h.Parent[:], h.MsgRoot[:], h.Timestamp); err != nil {
			if uniqueViolation(err) {
				return domErr(CodeDuplicateHeader, "competing header at height %d already stored", h.Height)
			}
			return err
		}

		// New tip landed: promote candidates now covered by confirmations.
		n, err := promoteCandidates(ctx, tx, h.ChainID)
		if err != nil {
			return err
		}
		if n > 0 {
			e.notify()
		}
		return nil
	})
	if err != nil {
		return err
	}
	if equivocationErr != nil {
		e.notify()
		return equivocationErr
	}
	return nil
}

// isFinal reports whether block at height h is finalized given the tip and
// the chain's confirmation depth (final when tip-h >= conf).
func isFinal(tipHeight, h uint64, conf int64) bool {
	return tipHeight >= uint64(conf) && h <= tipHeight-uint64(conf)
}

// handleEquivocation reacts to a competing header at a finalized height:
// freeze channels with executed messages affected, raise critical alerts,
// and return an error without mutating canonical history.
func (e *Executor) handleEquivocation(ctx context.Context, tx q, chain chainRow, h Header, occupant []byte) error {
	if err := raiseAlert(ctx, tx, "critical", "finalized_equivocation", h.ChainID, "",
		fmt.Sprintf("two different headers at finalized height %d: canonical=%x competing=%x",
			h.Height, occupant, h.Block[:])); err != nil {
		return err
	}
	// Freeze every open channel with an executed message coming from the
	// conflicting finalized block or a descendant of it.
	rows, err := tx.Query(ctx,
		`SELECT DISTINCT m.channel_id FROM messages m
		 JOIN headers hd ON hd.chain_id = m.chain_id AND hd.block_hash = m.block_hash
		 WHERE m.chain_id = $1 AND m.status = 'executed' AND hd.height >= $2`,
		h.ChainID, h.Height)
	if err != nil {
		return err
	}
	affected := map[string]bool{}
	for rows.Next() {
		var ch string
		if err := rows.Scan(&ch); err != nil {
			rows.Close()
			return err
		}
		affected[ch] = true
	}
	rows.Close()
	for chID := range affected {
		if err := freezeChannel(ctx, tx, h.ChainID, chID,
			fmt.Sprintf("frozen due to finalized equivocation at height %d", h.Height)); err != nil {
			return err
		}
		if err := raiseAlert(ctx, tx, "critical", "channel_frozen", h.ChainID, chID,
			fmt.Sprintf("channel frozen: executed message affected by finalized equivocation at height %d", h.Height)); err != nil {
			return err
		}
	}
	return domErr(CodeEquivocation,
		"FINALITY VIOLATION: competing header at finalized height %d; %d affected channel(s) frozen",
		h.Height, len(affected))
}
