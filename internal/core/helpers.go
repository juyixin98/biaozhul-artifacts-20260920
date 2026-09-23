package core

import (
	"context"
	"crypto/sha256"
	"encoding/binary"

	"inbox/internal/crypto"
)

// q is the transaction interface used by statement helpers.
type q = querier

// loadChainQ loads a chain row inside a transaction.
func loadChainQ(ctx context.Context, tx q, chainID string) (chainRow, error) {
	return loadChain(ctx, tx, chainID)
}

// advisoryKey derives a stable 64-bit advisory-lock key from a string.
func advisoryKey(kind, id string) int64 {
	h := sha256.Sum256(append(append([]byte(kind), 0), []byte(id)...))
	return int64(binary.BigEndian.Uint64(h[:8]))
}

func chainAdvKey(chainID string) int64 {
	return advisoryKey("chain/", chainID)
}

func channelAdvKey(chainID, channelID string) int64 {
	return advisoryKey("channel/", chainID+"/"+channelID)
}

// advisoryLock takes a transaction-scoped PostgreSQL advisory lock.
func advisoryLock(ctx context.Context, tx q, key int64) error {
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, key); err != nil {
		return err
	}
	return nil
}

// canonicalTip returns (height, hash, found) of the canonical active tip.
func canonicalTip(ctx context.Context, tx q, chainID string) (uint64, []byte, bool, error) {
	var height uint64
	var hash []byte
	err := tx.QueryRow(ctx,
		`SELECT height, block_hash FROM headers
		 WHERE chain_id = $1 AND canonical AND status = 'active'
		 ORDER BY height DESC LIMIT 1`,
		chainID).Scan(&height, &hash)
	if isNoRows(err) {
		return 0, nil, false, nil
	}
	if err != nil {
		return 0, nil, false, err
	}
	return height, hash, true, nil
}

func canonicalTipHeight(ctx context.Context, tx q, chainID string) (int64, error) {
	h, _, ok, err := canonicalTip(ctx, tx, chainID)
	if err != nil || !ok {
		return 0, err
	}
	return int64(h), nil
}

// promoteCandidates marks candidate messages as confirmed when their block
// is covered by the current confirmation depth of the chain. Returns the
// number promoted.
func promoteCandidates(ctx context.Context, tx q, chainID string) (int, error) {
	tag, err := tx.Exec(ctx,
		`UPDATE messages m
		 SET status = 'confirmed', updated_at = now()
		 FROM headers hd, chains c
		 WHERE m.chain_id = $1
		   AND m.status = 'candidate'
		   AND m.block_hash = hd.block_hash AND hd.chain_id = m.chain_id
		   AND hd.canonical AND hd.status = 'active'
		   AND c.id = m.chain_id
		   AND (SELECT COALESCE(MAX(height),0) FROM headers
		            WHERE chain_id = m.chain_id AND canonical AND status='active')
		       - hd.height >= c.confirmations`,
		chainID)
	if err != nil {
		return 0, err
	}
	return int(tag.RowsAffected()), nil
}

// raiseAlert inserts an operator-visible alert.
func raiseAlert(ctx context.Context, tx q, severity, kind, chainID, channelID, msg string) error {
	_, err := tx.Exec(ctx,
		`INSERT INTO alerts (severity, kind, chain_id, channel_id, message)
		 VALUES ($1,$2,$3,$4,$5)`,
		severity, kind, chainID, channelID, msg)
	return err
}

// freezeChannel marks a channel frozen, recording the reason. It is a
// one-way state transition: nothing in the inbox ever re-opens a frozen
// channel automatically.
func freezeChannel(ctx context.Context, tx q, chainID, channelID, reason string) error {
	tag, err := tx.Exec(ctx,
		`UPDATE channels SET status = 'frozen', frozen_reason = $3
		 WHERE chain_id = $1 AND channel_id = $2 AND status = 'open'`,
		chainID, channelID, reason)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		// Already frozen or missing: that's fine for alert fan-out.
		return nil
	}
	return nil
}

var zeroHash crypto.Hash
