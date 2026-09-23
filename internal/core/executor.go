package core

import (
	"context"
	"crypto/ed25519"
	"sync/atomic"

	"github.com/jackc/pgx/v5"

	"inbox/internal/crypto"
	"inbox/internal/store"
	"inbox/internal/wire"
)

// ChainConfig configures one simulated source chain.
type ChainConfig struct {
	ID            string
	ValidatorPub  ed25519.PublicKey
	Confirmations uint64
}

// Header is a source block header.
type Header struct {
	ChainID   string
	Height    uint64
	Block     crypto.Hash
	Parent    crypto.Hash
	MsgRoot   crypto.Hash
	Timestamp int64
}

// Message is a cross-chain message committed inside a source block.
type Message struct {
	ChainID     string
	ChannelID   string
	Nonce       uint64
	PayloadHash crypto.Hash
	Block       crypto.Hash
	SenderPub   ed25519.PublicKey
	SenderSig   []byte
}

// ChannelState is the runtime state of one channel.
type ChannelState struct {
	ChainID      string
	ChannelID    string
	NextNonce    uint64
	Status       string
	Senders      []ed25519.PublicKey
	FrozenReason string
}

// Revocation is validator-signed evidence that the current tip is void.
type Revocation struct {
	ChainID   string
	Block     crypto.Hash
	Validator ed25519.PublicKey
	Signature []byte
}

// CrashHook is invoked at well-defined points of the delivery transaction,
// used by the crash/restart tests. In production it is nil. point is
// "before_deliver" (channel locked, nothing written) or "before_commit"
// (all side effects staged, pre-COMMIT).
type CrashHook func(point, chainID, channelID string, nonce uint64)

// Executor is the inbox application core; all state lives in PostgreSQL.
type Executor struct {
	store *store.Store

	// crashHook, when non-nil, is invoked with "before_deliver" after the
	// channel lock is taken but before any side effect, and with
	// "before_commit" after every side effect but before COMMIT.
	crashHook CrashHook

	// crashArmed gates the hook: it only fires during an explicitly armed
	// processing tick (the test-driven /process call), never during normal
	// ingestion or the background worker.
	crashArmed atomic.Bool

	// wake is signalled whenever new data may make progress possible.
	wake chan struct{}
}

// NewExecutor builds an Executor over the given store.
func NewExecutor(s *store.Store) *Executor {
	return &Executor{store: s, wake: make(chan struct{}, 1)}
}

// SetCrashHook installs the test-only crash hook.
func (e *Executor) SetCrashHook(h CrashHook) { e.crashHook = h }

// ProcessArmed runs one processing tick with the crash hook enabled. The
// HTTP /process endpoint uses this so a configured INBOX_CRASH fires for
// the explicitly driven tick rather than for the background worker.
func (e *Executor) ProcessArmed(ctx context.Context) (int, error) {
	if e.crashHook == nil {
		return e.ProcessAll(ctx)
	}
	e.crashArmed.Store(true)
	defer e.crashArmed.Store(false)
	return e.ProcessAll(ctx)
}

// Wake returns a channel that is signalled whenever processing should run.
func (e *Executor) Wake() <-chan struct{} { return e.wake }

func (e *Executor) notify() {
	select {
	case e.wake <- struct{}{}:
	default:
	}
}

// SeedChains registers the simulated source chains. It is idempotent: an
// already-seeded chain must have the same validator key.
func (e *Executor) SeedChains(ctx context.Context, configs []ChainConfig) error {
	return e.store.InTx(ctx, func(tx pgx.Tx) error {
		for _, c := range configs {
			var existing []byte
			err := tx.QueryRow(ctx, `SELECT validator_pub FROM chains WHERE id = $1`, c.ID).Scan(&existing)
			if err == nil {
				if !bytesEqual(existing, c.ValidatorPub) {
					return domErr(CodeAlreadyExists, "chain %q already seeded with a different validator key", c.ID)
				}
				continue
			}
			if !isNoRows(err) {
				return err
			}
			if _, err := tx.Exec(ctx,
				`INSERT INTO chains (id, validator_pub, confirmations) VALUES ($1, $2, $3)`,
				c.ID, []byte(c.ValidatorPub), c.Confirmations); err != nil {
				return err
			}
		}
		return nil
	})
}

func bytesEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// View types used by read APIs and tests.
type (
	HeaderView   = wire.HeaderView
	MessageView  = wire.MessageView
	ChannelView  = wire.ChannelView
	DeliveryView = wire.DeliveryView
	AlertView    = wire.AlertView
)
