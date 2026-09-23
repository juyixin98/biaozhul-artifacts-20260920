// Package inbox implements the cross-chain message inbox state machine.
//
// Invariants enforced here:
//
//   - Message key = (source_chain, channel, sequence); the SHA-256 body
//     digest signed by the source fixture is immutable (see cryptoenvelope).
//   - Only messages anchored to FINAL blocks are executable.
//   - A sequence executes at most once (executions ledger + single
//     transaction that applies balances and flips status together).
//   - Out-of-order arrivals are staged; execution advances only over the
//     contiguous prefix from the next expected sequence.
//   - Revoking a not-yet-final source block cancels its candidate messages.
//   - Conflicting evidence for a key (same key, different body digest) is
//     never written over the stored row: it is preserved, the channel is
//     frozen and a critical alert is raised.
package inbox

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"

	"ximbox/internal/cryptoenvelope"
	"ximbox/internal/store"
)

// API-level sentinel errors, mapped to HTTP status codes in the server layer.
var (
	ErrBadRequest       = errors.New("bad request")
	ErrUnknownChain     = fmt.Errorf("%w: unknown source chain", ErrBadRequest)
	ErrFrozen           = errors.New("channel frozen")
	ErrFinalConflict    = store.ErrFinalConflict
	ErrBlockRevoked     = errors.New("block is revoked")
	ErrBlockMissing     = errors.New("block not found")
	ErrInvalidSignature = cryptoenvelope.ErrInvalidSignature
)

// CrashHook is used by crash/restart tests. When non-nil it is invoked after
// all SQL operations of one message execution have succeeded but BEFORE the
// transaction commits. Returning an error rolls the execution back; returning
// an exit code aborts the process, simulating a crash at that instant.
type CrashHook interface {
	BeforeCommit(chain, channel string, sequence int64) (exitCode *int, err error)
}

// Service is the inbox application service.
type Service struct {
	store   *store.Store
	signers map[string]ed25519.PublicKey
	crash   CrashHook
}

// New constructs a Service with the trusted fixture signer registry.
func New(s *store.Store, signers map[string]ed25519.PublicKey) *Service {
	return &Service{store: s, signers: signers}
}

// SetCrashHook installs a crash hook (tests).
func (s *Service) SetCrashHook(h CrashHook) { s.crash = h }

// Block proposal / finalization --------------------------------------------

// ProposeBlock ingests a (simulated) source-chain block.
//
// Reorg rule: proposing a live block at an already-occupied height with a
// different hash revokes every live block at that height or above and cancels
// their pending candidate messages. A different hash over an already FINAL
// height is refused (finality is one-way) — no silent rewrite.
func (s *Service) ProposeBlock(ctx context.Context, chain, hash, parentHash string, height int64) error {
	if err := validateChain(s.signers, chain); err != nil {
		return err
	}
	if err := validateHash(hash); err != nil {
		return fmt.Errorf("%w: hash: %v", ErrBadRequest, err)
	}
	if err := validateHash(parentHash); err != nil {
		return fmt.Errorf("%w: parent_hash: %v", ErrBadRequest, err)
	}
	if height < 1 {
		return fmt.Errorf("%w: height must be >= 1", ErrBadRequest)
	}

	return s.store.WithTx(ctx, func(q *store.Queries) error {
		// Same block already present? Idempotent no-op, but never ressurect a
		// revoked block through its old identity.
		existing, err := q.GetBlock(ctx, chain, hash)
		if err == nil {
			if existing.Status == "revoked" {
				return fmt.Errorf("%w: block %s was revoked by a reorg", ErrBlockRevoked, hash)
			}
			return nil
		} else if !errors.Is(err, store.ErrNotFound) {
			return err
		}

		// Parent must exist, be live, and sit exactly one height below
		// (genesis at height 1 may use the zero parent).
		if !(height == 1 && (parentHash == "0x0" || parentHash == zeroHash)) {
			parent, err := q.GetBlock(ctx, chain, parentHash)
			if errors.Is(err, store.ErrNotFound) {
				return store.ErrBadParent
			}
			if err != nil {
				return err
			}
			if parent.Status == "revoked" {
				return store.ErrBadParent
			}
			if parent.Height != height-1 {
				return fmt.Errorf("%w: parent height %d for child height %d",
					store.ErrBadParentHeight, parent.Height, height)
			}
		}

		conflict, live, err := q.BlockExistsAtHeight(ctx, chain, height)
		if err != nil {
			return err
		}
		if live && conflict.Hash == hash {
			return nil
		}
		if live && conflict.Status == "final" {
			return ErrFinalConflict
		}
		if live {
			// Reorg: revoke this height and everything built on top.
			if _, err := q.RevokeBlocksFrom(ctx, chain, height); err != nil {
				return err
			}
		}
		return q.InsertProposedBlock(ctx, chain, hash, parentHash, height)
	})
}

const zeroHash = "0x0000000000000000000000000000000000000000000000000000000000000000"

// ConfirmBlock marks a block and all of its proposed ancestors final, then
// advances every channel that has candidates anchored on newly-final blocks.
func (s *Service) ConfirmBlock(ctx context.Context, chain, hash string) (*store.Block, error) {
	if err := validateChain(s.signers, chain); err != nil {
		return nil, err
	}
	var channels []store.Channel
	err := s.store.WithTx(ctx, func(q *store.Queries) error {
		block, err := q.GetBlock(ctx, chain, hash)
		if errors.Is(err, store.ErrNotFound) {
			return ErrBlockMissing
		}
		if err != nil {
			return err
		}
		if block.Status == "revoked" {
			return ErrBlockRevoked
		}
		if block.Status == "proposed" {
			if err := q.FinalizeAncestors(ctx, chain, hash); err != nil {
				return err
			}
			block.Status = "final"
		}
		channels, err = q.ListActiveChannels(ctx)
		return err
	})
	if err != nil {
		return nil, err
	}
	// Execute any newly executable contiguous prefix outside the block tx;
	// each message advancement uses its own transaction.
	for i := range channels {
		if err := s.advanceChannel(ctx, channels[i].SourceChain, channels[i].ChannelID); err != nil {
			log.Printf("advance after confirm failed for %s/%s: %v",
				channels[i].SourceChain, channels[i].ChannelID, err)
		}
	}
	b, err := s.store.GetBlock(ctx, chain, hash)
	if err != nil {
		return nil, err
	}
	return b, nil
}

// Ingest -------------------------------------------------------------------

// IngestMessage verifies and stores an incoming signed message envelope.
//
// Duplicate handling for (chain, channel, seq):
//   - equal body digest: harmless re-delivery; the existing row is returned,
//     re-anchored to the new live block if it was pending/cancelled.
//   - different digest: conflicting evidence → evidence preserved, channel
//     frozen, alert raised, existing row left untouched.
func (s *Service) IngestMessage(ctx context.Context, e *cryptoenvelope.Envelope) (*store.Message, bool, error) {
	trusted, ok := s.signers[e.SourceChain]
	if !ok {
		return nil, false, ErrUnknownChain
	}
	if strings.TrimSpace(e.Channel) == "" || e.Sequence > maxSequence {
		return nil, false, fmt.Errorf("%w: invalid channel or sequence", ErrBadRequest)
	}
	if err := e.Verify(trusted); err != nil {
		if errors.Is(err, cryptoenvelope.ErrInvalidSignature) {
			return nil, false, ErrInvalidSignature
		}
		return nil, false, fmt.Errorf("%w: %v", ErrBadRequest, err)
	}

	// The anchor block must exist and be live.
	block, err := s.store.GetBlock(ctx, e.SourceChain, e.BlockHash)
	if errors.Is(err, store.ErrNotFound) {
		return nil, false, fmt.Errorf("%w: unknown block %s", ErrBadRequest, e.BlockHash)
	}
	if err != nil {
		return nil, false, err
	}
	if block.Status == "revoked" {
		return nil, false, fmt.Errorf("%w: block %s revoked", ErrBadRequest, e.BlockHash)
	}

	var (
		out       *store.Message
		frozeNow  bool
		advanceIt bool
	)
	err = s.store.WithTx(ctx, func(q *store.Queries) error {
		if err := q.EnsureChannel(ctx, e.SourceChain, e.Channel); err != nil {
			return err
		}
		ch, err := q.LockChannel(ctx, e.SourceChain, e.Channel)
		if err != nil {
			return err
		}
		if ch.Status == "frozen" {
			return fmt.Errorf("%w: %s", ErrFrozen, ch.FrozenReason)
		}

		existing, err := q.GetMessage(ctx, e.SourceChain, e.Channel, int64(e.Sequence))
		switch {
		case err == nil:
			if existing.BodyDigest == e.BodyDigest {
				// Same payload: re-delivery. Move with the chain if the old
				// anchor was revoked, otherwise stay anchored.
				if existing.Status == "pending" || existing.Status == "cancelled" {
					if existing.BlockHash != e.BlockHash {
						if err := q.ReanchorMessage(ctx, e.SourceChain, e.Channel,
							int64(e.Sequence), e.BlockHash); err != nil {
							return err
						}
						existing.BlockHash = e.BlockHash
						existing.Status = "pending"
					} else if existing.Status == "cancelled" {
						if err := q.ReanchorMessage(ctx, e.SourceChain, e.Channel,
							int64(e.Sequence), e.BlockHash); err != nil {
							return err
						}
						existing.Status = "pending"
					}
				}
				out = existing
				advanceIt = existing.Status == "pending" && block.Status == "final"
				return nil
			}
			// Conflicting evidence for the same key. Never overwrite.
			ev := &store.Evidence{
				SourceChain: e.SourceChain, ChannelID: e.Channel,
				Sequence: int64(e.Sequence), BlockHash: e.BlockHash,
				BodyDigest: e.BodyDigest, Body: e.Body, ExistingStatus: existing.Status,
			}
			first, err := q.InsertEvidence(ctx, ev)
			if err != nil {
				return err
			}
			kind := "equivocation_pending"
			if existing.Status == "executed" {
				kind = "equivocation_executed"
			}
			if first {
				detail := fmt.Sprintf(
					"conflicting copy for seq=%d: stored digest %s, observed digest %s",
					e.Sequence, existing.BodyDigest, e.BodyDigest)
				if err := q.InsertAlert(ctx, &store.Alert{
					SourceChain: e.SourceChain, ChannelID: e.Channel,
					Kind: kind, Sequence: seqPtr(int64(e.Sequence)),
					BodyDigest: e.BodyDigest, Detail: detail,
				}); err != nil {
					return err
				}
			}
			if err := q.FreezeChannel(ctx, e.SourceChain, e.Channel,
				"conflicting evidence at sequence "+strconv.FormatUint(e.Sequence, 10)); err != nil {
				return err
			}
			frozeNow = true
			out = existing
			return nil
		case !errors.Is(err, store.ErrNotFound):
			return err
		}

		m := &store.Message{
			SourceChain: e.SourceChain, ChannelID: e.Channel,
			Sequence: int64(e.Sequence), BlockHash: e.BlockHash,
			BodyDigest: e.BodyDigest, Body: e.Body, Status: "pending",
		}
		if err := q.InsertPendingMessage(ctx, m); err != nil {
			return err
		}
		out = m
		advanceIt = block.Status == "final"
		return nil
	})
	if err != nil {
		return nil, false, err
	}
	if advanceIt {
		if advErr := s.advanceChannel(ctx, e.SourceChain, e.Channel); advErr != nil {
			log.Printf("advance after ingest failed for %s/%s: %v",
				e.SourceChain, e.Channel, advErr)
		}
		// The row may have transitioned pending -> executed/execute_failed;
		// return the durable post-advancement state to the caller.
		if fresh, gerr := s.store.GetMessage(ctx, e.SourceChain, e.Channel, int64(e.Sequence)); gerr == nil {
			out = fresh
		}
	}
	return out, frozeNow, nil
}

// Advancement / execution --------------------------------------------------

// AdvanceChannel runs the prefix executor for one channel (manual trigger).
func (s *Service) AdvanceChannel(ctx context.Context, chain, channel string) error {
	if err := validateChain(s.signers, chain); err != nil {
		return err
	}
	return s.advanceChannel(ctx, chain, channel)
}

// RecoverOnStartup re-runs the prefix executor for every active channel,
// completing any work left pending by a crash mid-advancement.
func (s *Service) RecoverOnStartup(ctx context.Context) error {
	channels, err := s.store.ListActiveChannels(ctx)
	if err != nil {
		return err
	}
	for i := range channels {
		if err := s.advanceChannel(ctx, channels[i].SourceChain, channels[i].ChannelID); err != nil {
			log.Printf("startup recovery failed for %s/%s: %v",
				channels[i].SourceChain, channels[i].ChannelID, err)
		}
	}
	return nil
}

// advanceChannel executes the contiguous, final-block-anchored prefix.
//
// Each sequence is handled in its OWN transaction so that a process crash at
// any instant can only leave seq N committed or not committed — never twice.
// The sequence counter stops at the first gap, the first unconfirmed block,
// or the first failed application.
func (s *Service) advanceChannel(ctx context.Context, chain, channel string) error {
	for {
		stop, err := s.advanceOne(ctx, chain, channel)
		if err != nil {
			return err
		}
		if stop {
			return nil
		}
	}
}

// errAdvancementStops is not a real failure: failExecution returns it so the
// transaction COMMITS (failure mark + alert must persist) while the prefix
// loop stops afterwards.
var errAdvancementStops = errors.New("advancement stops")

var txOpts = pgx.TxOptions{}

// commitOn runs fn in a transaction; when fn returns the commitSentinel the
// transaction is committed (rather than rolled back) and stopped is reported.
func (s *Service) commitOn(ctx context.Context, commitSentinel error, fn func(q *store.Queries, tx pgx.Tx) error) (stopped bool, err error) {
	tx, beginErr := s.store.Pool.BeginTx(ctx, txOpts)
	if beginErr != nil {
		return false, beginErr
	}
	defer tx.Rollback(ctx) //nolint:errcheck // no-op after commit
	fnErr := fn(&store.Queries{DB: tx}, tx)
	if fnErr == nil {
		return false, tx.Commit(ctx)
	}
	if errors.Is(fnErr, commitSentinel) {
		if commitErr := tx.Commit(ctx); commitErr != nil {
			return false, commitErr
		}
		return true, nil
	}
	return false, fnErr
}

// txRollback explicitly rolls back (best effort) before a forced exit.
func txRollback(ctx context.Context, tx pgx.Tx) error { return tx.Rollback(ctx) }

// advanceOne executes at most the next expected pending message.
//
// Returns stop=true when the executor cannot make further progress.
func (s *Service) advanceOne(ctx context.Context, chain, channel string) (stop bool, err error) {
	stopped, txErr := s.commitOn(ctx, errAdvancementStops, func(q *store.Queries, tx pgx.Tx) error {
		ch, err := q.LockChannel(ctx, chain, channel)
		if errors.Is(err, store.ErrNotFound) {
			stop = true
			return nil
		}
		if err != nil {
			return err
		}
		if ch.Status == "frozen" {
			stop = true
			return nil
		}

		last, err := q.LastHandledSequence(ctx, chain, channel)
		if err != nil {
			return err
		}
		next := last + 1

		m, blockStatus, err := q.NextPendingMessage(ctx, chain, channel, next)
		if errors.Is(err, store.ErrNotFound) {
			// No pending candidate at or above next: gap, nothing to do.
			stop = true
			return nil
		}
		if err != nil {
			return err
		}
		if m.Sequence != next {
			// Earliest pending is beyond next -> there is a gap in between.
			stop = true
			return nil
		}
		if blockStatus != "final" {
			// Prefix is blocked waiting for block confirmation.
			stop = true
			return nil
		}

		// Real application: parse and validate the instruction. An invalid
		// instruction fails THIS message; later sequences cannot skip it.
		var body transferBody
		if jerr := json.Unmarshal(m.Body, &body); jerr != nil {
			return s.failExecution(ctx, q, m, "invalid body JSON: "+jerr.Error())
		}
		if jerr := body.validate(); jerr != nil {
			return s.failExecution(ctx, q, m, jerr.Error())
		}

		// Apply the balance transition inside this same transaction.
		if err := q.AdjustBalance(ctx, body.To, body.Amount); err != nil {
			return s.failExecution(ctx, q, m, "balance apply failed: "+err.Error())
		}
		if err := q.MarkExecuted(ctx, chain, channel, m.BodyDigest, m.Sequence); err != nil {
			return err
		}

		// Crash injection point: SQL done, commit not yet issued.
		if s.crash != nil {
			code, hookErr := s.crash.BeforeCommit(chain, channel, m.Sequence)
			if hookErr != nil {
				return s.failExecution(ctx, q, m, "crash hook: "+hookErr.Error())
			}
			if code != nil {
				log.Printf("CRASH INJECTION: exiting with code %d after applying seq=%d, before commit",
					*code, m.Sequence)
				_ = txRollback(ctx, tx)
				os.Exit(*code)
			}
		}
		return nil
	})
	if txErr != nil {
		return false, txErr
	}
	return stop || stopped, nil
}

// failExecution marks the message failed, raises an alert, and signals the
// advancement loop to stop. It runs inside the caller's transaction.
func (s *Service) failExecution(ctx context.Context, q *store.Queries, m *store.Message, reason string) error {
	if err := q.MarkExecutionFailed(ctx, m.SourceChain, m.ChannelID, m.Sequence, reason); err != nil {
		return err
	}
	if err := q.InsertAlert(ctx, &store.Alert{
		SourceChain: m.SourceChain, ChannelID: m.ChannelID,
		Kind: "execution_failed", Sequence: seqPtr(m.Sequence),
		BodyDigest: m.BodyDigest, Detail: reason,
	}); err != nil {
		return err
	}
	// Encode "stop" via the sentinel consumed by the transaction wrapper.
	return errAdvancementStops
}

// transferBody is the only instruction the simulated application understands:
// credit `amount` units to `to`. The application state is the accounts table;
// the sum of committed transfer amounts is observable after execution.
type transferBody struct {
	Type   string `json:"type"`
	To     string `json:"to"`
	Amount int64  `json:"amount"`
}

func (b *transferBody) validate() error {
	if b.Type != "transfer" {
		return fmt.Errorf("unsupported body type %q (want \"transfer\")", b.Type)
	}
	if strings.TrimSpace(b.To) == "" {
		return errors.New("transfer: missing \"to\"")
	}
	if b.Amount <= 0 {
		return fmt.Errorf("transfer: amount must be positive, got %d", b.Amount)
	}
	return nil
}

// Small shared helpers -----------------------------------------------------

const maxSequence = 1<<63 - 1

func validateChain(signers map[string]ed25519.PublicKey, chain string) error {
	if _, ok := signers[chain]; !ok {
		return ErrUnknownChain
	}
	return nil
}

func validateHash(h string) error {
	if len(h) != 66 || h[:2] != "0x" {
		return errors.New("hash must be 0x + 64 hex chars")
	}
	for i := 2; i < len(h); i++ {
		c := h[i]
		switch {
		case c >= '0' && c <= '9', c >= 'a' && c <= 'f', c >= 'A' && c <= 'F':
		default:
			return errors.New("hash contains non-hex character")
		}
	}
	return nil
}

func seqPtr(v int64) *int64 { return &v }
