package core

import (
	"context"
	"crypto/ed25519"
	"encoding/hex"
	"errors"
	"strings"
)

var (
	errNothingReady  = errors.New("nothing ready at next nonce")
	errChannelClosed = errors.New("channel is not open")
)

func b2h(b []byte) string { return hex.EncodeToString(b) }

// ListMessages returns the messages of a channel ordered by nonce.
func (e *Executor) ListMessages(ctx context.Context, chainID, channelID string) ([]MessageView, error) {
	rows, err := e.store.Pool.Query(ctx,
		`SELECT chain_id, channel_id, nonce, payload_hash, block_hash, status
		 FROM messages WHERE chain_id=$1 AND channel_id=$2
		 ORDER BY nonce, id`,
		chainID, channelID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []MessageView
	for rows.Next() {
		var v MessageView
		var payload, block []byte
		if err := rows.Scan(&v.ChainID, &v.ChannelID, &v.Nonce, &payload, &block, &v.Status); err != nil {
			return nil, err
		}
		v.PayloadHashHex = b2h(payload)
		v.BlockHex = b2h(block)
		out = append(out, v)
	}
	return out, rows.Err()
}

// ListDeliveries returns the executed delivery records of a channel.
func (e *Executor) ListDeliveries(ctx context.Context, chainID, channelID string) ([]DeliveryView, error) {
	rows, err := e.store.Pool.Query(ctx,
		`SELECT chain_id, channel_id, nonce, block_hash, attempts
		 FROM deliveries WHERE chain_id=$1 AND channel_id=$2
		 ORDER BY nonce`,
		chainID, channelID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []DeliveryView
	for rows.Next() {
		var v DeliveryView
		var block []byte
		if err := rows.Scan(&v.ChainID, &v.ChannelID, &v.Nonce, &block, &v.Attempts); err != nil {
			return nil, err
		}
		v.BlockHex = b2h(block)
		out = append(out, v)
	}
	return out, rows.Err()
}

// ListHeaders returns canonical/active headers of a chain (plus optionally
// revoked ones when includeRevoked is true).
func (e *Executor) ListHeaders(ctx context.Context, chainID string, includeRevoked bool) ([]HeaderView, error) {
	q := `SELECT chain_id, height, block_hash, parent_hash, msg_root, canonical, status::text
	      FROM headers WHERE chain_id=$1`
	if !includeRevoked {
		q += ` AND status='active' AND canonical`
	}
	q += ` ORDER BY height, id`
	rows, err := e.store.Pool.Query(ctx, q, chainID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []HeaderView
	for rows.Next() {
		var v HeaderView
		var block, parent, root []byte
		var status string
		if err := rows.Scan(&v.ChainID, &v.Height, &block, &parent, &root, &v.Canonical, &status); err != nil {
			return nil, err
		}
		v.BlockHex = b2h(block)
		v.ParentHex = b2h(parent)
		v.MsgRootHex = b2h(root)
		v.Status = status
		out = append(out, v)
	}
	return out, rows.Err()
}

// ListAlerts returns alerts, optionally filtered by chain and severity.
func (e *Executor) ListAlerts(ctx context.Context, chainID, severity string, limit int) ([]AlertView, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	var conds []string
	var args []any
	if chainID != "" {
		args = append(args, chainID)
		conds = append(conds, "chain_id = $"+itoa(len(args)))
	}
	if severity != "" {
		args = append(args, severity)
		conds = append(conds, "severity = $"+itoa(len(args)))
	}
	q := "SELECT id, severity, kind, chain_id, COALESCE(channel_id,''), message, to_char(created_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.MSZ') FROM alerts"
	if len(conds) > 0 {
		q += " WHERE " + strings.Join(conds, " AND ")
	}
	args = append(args, limit)
	q += " ORDER BY id DESC LIMIT $" + itoa(len(args))
	rows, err := e.store.Pool.Query(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []AlertView
	for rows.Next() {
		var v AlertView
		if err := rows.Scan(&v.ID, &v.Severity, &v.Kind, &v.ChainID, &v.ChannelID, &v.Message, &v.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// ValidatorPub returns the registered validator public key of a chain.
func (e *Executor) ValidatorPub(ctx context.Context, chainID string) (ed25519.PublicKey, error) {
	var pub []byte
	err := e.store.Pool.QueryRow(ctx, `SELECT validator_pub FROM chains WHERE id=$1`, chainID).Scan(&pub)
	if isNoRows(err) {
		return nil, domErr(CodeUnknownChain, "unknown chain %q", chainID)
	}
	if err != nil {
		return nil, err
	}
	return ed25519.PublicKey(pub), nil
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}
