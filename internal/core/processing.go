package core

import (
	"context"
	"fmt"
)

// ProcessAll runs one processing tick across every open channel and returns
// the total number of messages delivered.
//
// Delivery is the (idempotent, transactional) application side effect:
// appending a row to deliveries and advancing the channel's next_nonce.
// Both happen in the same SERIALIZABLE transaction as the message status
// transition, so a crash at any point leaves either everything or nothing
// committed — the message is never executed twice.
func (e *Executor) ProcessAll(ctx context.Context) (int, error) {
	rows, err := e.store.Pool.Query(ctx,
		`SELECT chain_id, channel_id FROM channels WHERE status = 'open'`)
	if err != nil {
		return 0, err
	}
	type ch struct{ chain, channel string }
	var channels []ch
	for rows.Next() {
		var c ch
		if err := rows.Scan(&c.chain, &c.channel); err != nil {
			rows.Close()
			return 0, err
		}
		channels = append(channels, c)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, err
	}

	total := 0
	for _, c := range channels {
		n, err := e.processChannel(ctx, c.chain, c.channel)
		if err != nil {
			return total, fmt.Errorf("process %s/%s: %w", c.chain, c.channel, err)
		}
		total += n
	}
	return total, nil
}

// processChannel advances one channel over its longest confirmed prefix.
func (e *Executor) processChannel(ctx context.Context, chainID, channelID string) (int, error) {
	delivered := 0
	for {
		// Each message is committed independently so the crash hook can
		// kill the process between the side effect and its COMMIT.
		advanced, err := e.deliverOne(ctx, chainID, channelID)
		if err != nil {
			return delivered, err
		}
		if !advanced {
			return delivered, nil
		}
		delivered++
	}
}

// deliverOne attempts to deliver exactly the message at the channel's
// next_nonce. Returns (true, nil) when it committed a delivery.
func (e *Executor) deliverOne(ctx context.Context, chainID, channelID string) (bool, error) {
	committed := false
	err := e.inTx(ctx, func(tx q) error {
		if err := advisoryLock(ctx, tx, channelAdvKey(chainID, channelID)); err != nil {
			return err
		}

		var nextNonce uint64
		var chStatus string
		if err := tx.QueryRow(ctx,
			`SELECT next_nonce, status FROM channels WHERE chain_id=$1 AND channel_id=$2`,
			chainID, channelID).Scan(&nextNonce, &chStatus); err != nil {
			return err
		}
		if chStatus != "open" {
			return errChannelClosed
		}

		// Only confirmed messages (bound to finalized source blocks) run.
		var blockHash []byte
		err := tx.QueryRow(ctx,
			`SELECT block_hash FROM messages
			 WHERE chain_id=$1 AND channel_id=$2 AND nonce=$3 AND status='confirmed'`,
			chainID, channelID, nextNonce).Scan(&blockHash)
		if isNoRows(err) {
			// Gap, or nothing ready: prefix stops here. Later confirmed
			// (but out-of-order) nonces stay staged and wait.
			return errNothingReady
		}
		if err != nil {
			return err
		}

		if e.crashHook != nil && e.crashArmed.Load() {
			// "before_deliver": lock held, nothing written yet. A process
			// killed here must, on restart, deliver this message once.
			e.crashHook("before_deliver", chainID, channelID, nextNonce)
		}

		// The application side effect. The UNIQUE (chain, channel, nonce)
		// constraint on deliveries makes this exactly-once even under
		// retries.
		if _, err := tx.Exec(ctx,
			`INSERT INTO deliveries (chain_id, channel_id, nonce, block_hash, attempts)
			 VALUES ($1,$2,$3,$4,1)
			 ON CONFLICT (chain_id, channel_id, nonce)
			 DO UPDATE SET attempts = deliveries.attempts + 1`,
			chainID, channelID, nextNonce, blockHash); err != nil {
			return err
		}

		if _, err := tx.Exec(ctx,
			`UPDATE messages SET status='executed', updated_at=now()
			 WHERE chain_id=$1 AND channel_id=$2 AND nonce=$3 AND status='confirmed'`,
			chainID, channelID, nextNonce); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx,
			`UPDATE channels SET next_nonce=$3 WHERE chain_id=$1 AND channel_id=$2`,
			chainID, channelID, nextNonce+1); err != nil {
			return err
		}

		if e.crashHook != nil && e.crashArmed.Load() {
			// "before_commit": every side effect is staged in this
			// transaction. A process killed here rolls back atomically;
			// restart delivers exactly once.
			e.crashHook("before_commit", chainID, channelID, nextNonce)
		}

		committed = true
		return nil
	})
	if err == errNothingReady || err == errChannelClosed {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return committed, nil
}
