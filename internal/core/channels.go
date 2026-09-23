package core

import (
	"context"
	"crypto/ed25519"
)

// OpenChannel opens a delivery channel on a source chain and registers the
// set of sender public keys allowed to post messages into it.
func (e *Executor) OpenChannel(ctx context.Context, chainID, channelID string, senders []ed25519.PublicKey) error {
	if channelID == "" {
		return domErr(CodeBadRequest, "channel_id is required")
	}
	if len(senders) == 0 {
		return domErr(CodeBadRequest, "at least one sender public key is required")
	}
	for _, s := range senders {
		if len(s) != ed25519.PublicKeySize {
			return domErr(CodeBadRequest, "sender public key has wrong length")
		}
	}
	return e.inTx(ctx, func(tx q) error {
		if _, err := loadChainQ(ctx, tx, chainID); err != nil {
			return err
		}
		var existing string
		err := tx.QueryRow(ctx,
			`SELECT status FROM channels WHERE chain_id = $1 AND channel_id = $2`,
			chainID, channelID).Scan(&existing)
		if err == nil {
			return domErr(CodeAlreadyExists, "channel %q on %q already exists (status %s)", channelID, chainID, existing)
		}
		if !isNoRows(err) {
			return err
		}
		if _, err := tx.Exec(ctx,
			`INSERT INTO channels (chain_id, channel_id) VALUES ($1,$2)`,
			chainID, channelID); err != nil {
			return err
		}
		for _, s := range senders {
			if _, err := tx.Exec(ctx,
				`INSERT INTO channel_senders (chain_id, channel_id, sender_pub) VALUES ($1,$2,$3)`,
				chainID, channelID, []byte(s)); err != nil {
				return err
			}
		}
		return nil
	})
}

// GetChannel returns the state of one channel.
func (e *Executor) GetChannel(ctx context.Context, chainID, channelID string) (ChannelState, error) {
	var cs ChannelState
	err := e.store.Pool.QueryRow(ctx,
		`SELECT chain_id, channel_id, next_nonce, status, COALESCE(frozen_reason,'')
		 FROM channels WHERE chain_id = $1 AND channel_id = $2`,
		chainID, channelID).
		Scan(&cs.ChainID, &cs.ChannelID, &cs.NextNonce, &cs.Status, &cs.FrozenReason)
	if isNoRows(err) {
		return cs, domErr(CodeUnknownChannel, "channel %q on %q not found", channelID, chainID)
	}
	if err != nil {
		return cs, err
	}
	rows, err := e.store.Pool.Query(ctx,
		`SELECT sender_pub FROM channel_senders WHERE chain_id = $1 AND channel_id = $2`,
		chainID, channelID)
	if err != nil {
		return cs, err
	}
	defer rows.Close()
	for rows.Next() {
		var pub []byte
		if err := rows.Scan(&pub); err != nil {
			return cs, err
		}
		cs.Senders = append(cs.Senders, ed25519.PublicKey(pub))
	}
	return cs, rows.Err()
}
