package core

import (
	"context"
	"fmt"

	"inbox/internal/crypto"
	"inbox/internal/merkle"
)

// SubmitMessage ingests a cross-chain message.
//
// Authenticity is fully verified:
//  1. The sender Ed25519 signature over (channel, nonce, payloadHash) must
//     verify against a public key registered on the channel.
//  2. The message commitment must be included in the block's Merkle root
//     via the supplied proof.
//  3. The referenced block must be a canonical, active header.
//
// Duplicates with the same key (chain, channel, nonce):
//   - Same body (payload hash): idempotent acceptance; a body is immutable.
//   - Different body: conflict. If the existing message was already
//     executed, the channel is frozen and a critical alert raised — the
//     conflicting copy is rejected and executed history is never rewritten.
//     Otherwise a conflict alert is raised and the copy rejected.
func (e *Executor) SubmitMessage(ctx context.Context, m Message, proof merkle.Proof) error {
	if m.Nonce == 0 {
		return domErr(CodeBadRequest, "nonce must be >= 1")
	}
	commitment := crypto.MessageCommitment(m.ChannelID, m.Nonce, m.PayloadHash)
	if err := crypto.Verify(m.SenderPub, commitment, m.SenderSig); err != nil {
		return domErr(CodeBadSignature, "sender signature: %v", err)
	}

	var conflictErr error
	err := e.inTx(ctx, func(tx q) error {
		chain, err := loadChainQ(ctx, tx, m.ChainID)
		if err != nil {
			return err
		}
		if err := advisoryLock(ctx, tx, channelAdvKey(m.ChainID, m.ChannelID)); err != nil {
			return err
		}

		var chStatus string
		err = tx.QueryRow(ctx,
			`SELECT status FROM channels WHERE chain_id = $1 AND channel_id = $2`,
			m.ChainID, m.ChannelID).Scan(&chStatus)
		if isNoRows(err) {
			return domErr(CodeUnknownChannel, "channel %q on %q not found", m.ChannelID, m.ChainID)
		}
		if err != nil {
			return err
		}
		if chStatus == "frozen" {
			return domErr(CodeChannelFrozen, "channel %q is frozen due to a conflict; no new messages accepted", m.ChannelID)
		}

		var registered bool
		err = tx.QueryRow(ctx,
			`SELECT EXISTS(SELECT 1 FROM channel_senders WHERE chain_id=$1 AND channel_id=$2 AND sender_pub=$3)`,
			m.ChainID, m.ChannelID, []byte(m.SenderPub)).Scan(&registered)
		if err != nil {
			return err
		}
		if !registered {
			return domErr(CodeBadSignature, "sender public key is not registered on channel %q", m.ChannelID)
		}

		var hStatus, hCanonical string
		var msgRoot []byte
		var height uint64
		err = tx.QueryRow(ctx,
			`SELECT status, canonical::text, msg_root, height FROM headers WHERE chain_id = $1 AND block_hash = $2`,
			m.ChainID, m.Block[:]).Scan(&hStatus, &hCanonical, &msgRoot, &height)
		if isNoRows(err) {
			return domErr(CodeBlockUnknown, "containing block %x is not known", m.Block[:])
		}
		if err != nil {
			return err
		}
		if hStatus != "active" || hCanonical != "true" {
			return domErr(CodeBlockNotCanonical, "containing block is not canonical/active")
		}
		var root crypto.Hash
		copy(root[:], msgRoot)
		if err := merkle.Verify(root, commitment, proof); err != nil {
			return domErr(CodeBadProof, "%v", err)
		}

		// Duplicate / conflict detection on the message key.
		var existingPayload []byte
		var existingStatus string
		err = tx.QueryRow(ctx,
			`SELECT payload_hash, status FROM messages
			 WHERE chain_id = $1 AND channel_id = $2 AND nonce = $3 AND status <> 'cancelled'`,
			m.ChainID, m.ChannelID, m.Nonce).Scan(&existingPayload, &existingStatus)
		switch {
		case err == nil:
			if bytesEqual(existingPayload, m.PayloadHash[:]) {
				// Identical duplicate: idempotent. The body is immutable;
				// we never re-execute (the delivery key is unique).
				return nil
			}
			// Conflicting body for the same key. The freeze + alert side
			// effects are written in THIS transaction and COMMITTED by
			// returning nil; conflictErr is surfaced afterwards. They must
			// never be rolled back — incidents are not silently undone.
			conflictErr = e.handleMessageConflict(ctx, tx, m, existingStatus, existingPayload)
			return nil
		case !isNoRows(err):
			return err
		}

		// Determine whether the containing block is already finalized.
		tipHeight, _, _, err := canonicalTip(ctx, tx, m.ChainID)
		if err != nil {
			return err
		}
		status := "candidate"
		if isFinal(tipHeight, height, chain.confirmations) {
			status = "confirmed"
		}

		if _, err := tx.Exec(ctx,
			`INSERT INTO messages (chain_id, channel_id, nonce, payload_hash, block_hash, sender_pub, sender_sig, status)
			 VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
			m.ChainID, m.ChannelID, m.Nonce, m.PayloadHash[:], m.Block[:],
			[]byte(m.SenderPub), m.SenderSig, status); err != nil {
			if uniqueViolation(err) {
				// Concurrent identical insert — treat as idempotent.
				return nil
			}
			return err
		}
		return nil
	})
	if err != nil {
		return err
	}
	if conflictErr != nil {
		// Incident (freeze + alert) already durably committed above.
		return conflictErr
	}
	e.notify()
	return nil
}

// handleMessageConflict handles a duplicate key with a different immutable
// body. Executed existing -> freeze channel; otherwise alert only.
func (e *Executor) handleMessageConflict(ctx context.Context, tx q, m Message, existingStatus string, existingPayload []byte) error {
	detail := fmt.Sprintf(
		"conflicting message for key (%s,%s,%d): stored payload=%x incoming payload=%x (stored status=%s)",
		m.ChainID, m.ChannelID, m.Nonce, existingPayload, m.PayloadHash[:], existingStatus)

	if existingStatus == "executed" {
		if err := raiseAlert(ctx, tx, "critical", "message_conflict_executed", m.ChainID, m.ChannelID,
			detail+"; executed message cannot be rewritten — freezing channel"); err != nil {
			return err
		}
		if err := freezeChannel(ctx, tx, m.ChainID, m.ChannelID,
			fmt.Sprintf("conflicting evidence for executed nonce %d", m.Nonce)); err != nil {
			return err
		}
		return domErr(CodeConflictFrozen,
			"CONFLICT: nonce %d was already executed with a different body; channel frozen and incident alerted",
			m.Nonce)
	}

	if err := raiseAlert(ctx, tx, "critical", "message_conflict", m.ChainID, m.ChannelID,
		detail+"; conflicting copy rejected (no state rewritten)"); err != nil {
		return err
	}
	return domErr(CodeConflict,
		"CONFLICT: nonce %d already has a different body; conflicting copy rejected and incident alerted",
		m.Nonce)
}
