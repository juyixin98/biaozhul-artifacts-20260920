// Integration tests for the inbox state machine. They require a real
// PostgreSQL database (default postgres://ximbox:ximbox_dev_pwd@localhost:
// 5432/ximbox_test). Set XIMBOX_TEST_DATABASE_URL to override. If the
// database is unreachable the tests skip rather than fake a pass.
package tests

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"ximbox/internal/cryptoenvelope"
	"ximbox/internal/inbox"
	"ximbox/internal/store"
)

type harness struct {
	t    *testing.T
	ctx  context.Context
	pool *pgxpool.Pool
	st   *store.Store
	svc  *inbox.Service
	keys map[string]cryptoenvelope.FixtureKey
}

func testDBURL() string {
	if v := os.Getenv("XIMBOX_TEST_DATABASE_URL"); v != "" {
		return v
	}
	return "postgres://ximbox:ximbox_dev_pwd@localhost:5432/ximbox_test?sslmode=disable"
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, testDBURL())
	if err != nil {
		t.Skipf("connect test db: %v", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		t.Skipf("ping test db (set XIMBOX_TEST_DATABASE_URL): %v", err)
	}
	t.Cleanup(pool.Close)

	st := store.New(pool)
	if err := st.Migrate(ctx); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	if err := st.TruncateAll(ctx); err != nil {
		t.Fatalf("truncate: %v", err)
	}
	svc := inbox.New(st, cryptoenvelope.TrustedPublicKeys())
	return &harness{
		t: t, ctx: ctx, pool: pool, st: st, svc: svc,
		keys: cryptoenvelope.TrustedFixtures(),
	}
}

// --- fixture builders ------------------------------------------------------

// blockHashes for chain c: h[n] is the canonical hash at height n.
func (h *harness) blockHash(chain string, n int) string {
	parent := cryptoenvelope.ZeroHash
	if n > 1 {
		parent = h.blockHash(chain, n-1)
	}
	return cryptoenvelope.SimBlockHash(chain, int64(n), parent, "")
}

// competingHash is a reorged (different) hash at height n with extras salt.
func (h *harness) competingHash(chain string, n int, salt string) string {
	parent := cryptoenvelope.ZeroHash
	if n > 1 {
		parent = h.blockHash(chain, n-1)
	}
	return cryptoenvelope.SimBlockHash(chain, int64(n), parent, salt)
}

func (h *harness) propose(chain string, n int) string {
	h.t.Helper()
	hash := h.blockHash(chain, n)
	parent := cryptoenvelope.ZeroHash
	if n > 1 {
		parent = h.blockHash(chain, n-1)
	}
	if err := h.svc.ProposeBlock(h.ctx, chain, hash, parent, int64(n)); err != nil {
		h.t.Fatalf("propose block %d: %v", n, err)
	}
	return hash
}

func (h *harness) proposeCompeting(chain string, n int, salt string) string {
	h.t.Helper()
	parent := cryptoenvelope.ZeroHash
	if n > 1 {
		parent = h.blockHash(chain, n-1)
	}
	hash := cryptoenvelope.SimBlockHash(chain, int64(n), parent, salt)
	if err := h.svc.ProposeBlock(h.ctx, chain, hash, parent, int64(n)); err != nil {
		h.t.Fatalf("propose competing block %d (%s): %v", n, salt, err)
	}
	return hash
}

func (h *harness) confirm(chain string, n int) {
	h.t.Helper()
	if _, err := h.svc.ConfirmBlock(h.ctx, chain, h.blockHash(chain, n)); err != nil {
		h.t.Fatalf("confirm block %d: %v", n, err)
	}
}

func (h *harness) confirmHash(chain, hash string) {
	h.t.Helper()
	if _, err := h.svc.ConfirmBlock(h.ctx, chain, hash); err != nil {
		h.t.Fatalf("confirm block %s: %v", hash, err)
	}
}

func transferBody(to string, amount int64) json.RawMessage {
	return json.RawMessage(fmt.Sprintf(`{"type":"transfer","to":%q,"amount":%d}`, to, amount))
}

// signMsg produces a genuinely signed envelope anchored on block n.
func (h *harness) signMsg(chain, channel string, seq uint64, blockN int, body json.RawMessage) *cryptoenvelope.Envelope {
	h.t.Helper()
	env, err := cryptoenvelope.Sign(h.keys[chain].Private, chain, channel,
		h.blockHash(chain, blockN), seq, body)
	if err != nil {
		h.t.Fatalf("sign: %v", err)
	}
	return env
}

// signMsgHash signs anchored to an explicit block hash.
func (h *harness) signMsgHash(chain, channel string, seq uint64, blockHash string, body json.RawMessage) *cryptoenvelope.Envelope {
	h.t.Helper()
	env, err := cryptoenvelope.Sign(h.keys[chain].Private, chain, channel, blockHash, seq, body)
	if err != nil {
		h.t.Fatalf("sign: %v", err)
	}
	return env
}

func (h *harness) ingest(env *cryptoenvelope.Envelope) *store.Message {
	h.t.Helper()
	m, _, err := h.svc.IngestMessage(h.ctx, env)
	if err != nil {
		h.t.Fatalf("ingest seq=%d: %v", env.Sequence, err)
	}
	return m
}

func (h *harness) ingestErr(env *cryptoenvelope.Envelope) error {
	_, _, err := h.svc.IngestMessage(h.ctx, env)
	return err
}

func (h *harness) msgStatus(chain, channel string, seq int64) string {
	h.t.Helper()
	m, err := h.st.GetMessage(h.ctx, chain, channel, seq)
	if err != nil {
		h.t.Fatalf("get message %d: %v", seq, err)
	}
	return m.Status
}

func (h *harness) balance(addr string) int64 {
	h.t.Helper()
	a, err := h.st.GetAccount(h.ctx, addr)
	if errors.Is(err, store.ErrNotFound) {
		return 0
	}
	if err != nil {
		h.t.Fatalf("get account %s: %v", addr, err)
	}
	return a.Balance
}

func (h *harness) channelStatus(chain, channel string) string {
	h.t.Helper()
	c, err := h.st.GetChannel(h.ctx, chain, channel)
	if err != nil {
		h.t.Fatalf("get channel: %v", err)
	}
	return c.Status
}

// TestGapFill: out-of-order arrivals stay pending, then the prefix advances.
func TestGapFill(t *testing.T) {
	h := newHarness(t)
	const chain, ch = cryptoenvelope.ChainA, "orders"

	// Two blocks, but confirm only block 1.
	h.propose(chain, 1)
	h.propose(chain, 2)
	h.confirm(chain, 1)

	// seq 1 arrives first and is anchored on still-proposed block 2; it must
	// stage. seq 0 lands on the already-final block 1 and executes right away.
	m1 := h.ingest(h.signMsg(chain, ch, 1, 2, transferBody("bob", 5)))
	if m1.Status != "pending" {
		t.Fatalf("seq1 on proposed block should be pending, got %q", m1.Status)
	}
	if got := h.balance("bob"); got != 0 {
		t.Fatalf("unconfirmed seq1 must not execute, bob=%d", got)
	}
	m0 := h.ingest(h.signMsg(chain, ch, 0, 1, transferBody("alice", 10)))
	if m0.Status != "executed" {
		t.Fatalf("seq0 on final block should execute immediately, got %q", m0.Status)
	}
	if got := h.balance("alice"); got != 10 {
		t.Fatalf("alice=%d want 10 after seq0 executed", got)
	}

	// Confirming block 2 confirms the ancestor-less chain and must run the
	// contiguous prefix 0 then 1 in order.
	h.confirm(chain, 2)
	if s := h.msgStatus(chain, ch, 0); s != "executed" {
		t.Fatalf("seq0 status=%s", s)
	}
	if s := h.msgStatus(chain, ch, 1); s != "executed" {
		t.Fatalf("seq1 status=%s", s)
	}
	if got := h.balance("alice"); got != 10 {
		t.Fatalf("alice=%d want 10", got)
	}
	if got := h.balance("bob"); got != 5 {
		t.Fatalf("bob=%d want 5", got)
	}
}

// TestDuplicateExecutesOnce: same sequence + same digest re-delivered any
// number of times executes the body exactly once.
func TestDuplicateExecutesOnce(t *testing.T) {
	h := newHarness(t)
	const chain, ch = cryptoenvelope.ChainA, "dup"
	h.propose(chain, 1)
	h.confirm(chain, 1)

	env := h.signMsg(chain, ch, 0, 1, transferBody("alice", 10))
	h.ingest(env)
	// Identical re-delivery before AND after execution.
	h.ingest(env)
	if err := h.svc.AdvanceChannel(h.ctx, chain, ch); err != nil {
		t.Fatal(err)
	}
	h.ingest(env)
	h.ingest(env)

	if got := h.balance("alice"); got != 10 {
		t.Fatalf("duplicate delivery applied multiple times: alice=%d want 10", got)
	}
	executed, err := h.st.HasExecution(h.ctx, chain, ch, 0)
	if err != nil || !executed {
		t.Fatalf("execution ledger missing: executed=%v err=%v", executed, err)
	}
}

// TestConflictingCopyFreezesChannel: after seq 0 executed, a second copy with
// a different signed body must be preserved as evidence and freeze the
// channel; the stored row must be left untouched.
func TestConflictingCopyFreezesChannel(t *testing.T) {
	h := newHarness(t)
	const chain, ch = cryptoenvelope.ChainA, "freeze-me"
	h.propose(chain, 1)
	h.confirm(chain, 1)

	h.ingest(h.signMsg(chain, ch, 0, 1, transferBody("alice", 10)))
	original := h.ingest(h.signMsg(chain, ch, 0, 1, transferBody("alice", 10)))

	// A genuinely different body, also validly signed by the source (equivocation).
	conflict := h.signMsg(chain, ch, 0, 1, transferBody("mallory", 999))
	m, froze, err := h.svc.IngestMessage(h.ctx, conflict)
	if err != nil {
		t.Fatalf("conflicting ingest should be recorded, got err=%v", err)
	}
	if !froze {
		t.Fatal("expected channel_frozen=true on conflicting evidence")
	}
	// Stored message must still be the ORIGINAL digest, never overwritten.
	if m.BodyDigest != original.BodyDigest {
		t.Fatalf("stored digest was overwritten: %s", m.BodyDigest)
	}
	if s := h.channelStatus(chain, ch); s != "frozen" {
		t.Fatalf("channel status=%s want frozen", s)
	}
	// Evidence preserved and an alert raised.
	evidence, err := h.st.ListEvidence(h.ctx, chain, ch, 10)
	if err != nil || len(evidence) != 1 {
		t.Fatalf("evidence len=%d err=%v", len(evidence), err)
	}
	if evidence[0].BodyDigest != conflict.BodyDigest {
		t.Fatal("evidence digest mismatch")
	}
	alerts, err := h.st.ListAlerts(h.ctx, chain, ch, 10)
	if err != nil || len(alerts) != 1 || alerts[0].Kind != "equivocation_executed" {
		t.Fatalf("alerts=%v err=%v", alerts, err)
	}
	// Frozen channel rejects further ingestions.
	if err := h.ingestErr(h.signMsg(chain, ch, 1, 1, transferBody("bob", 1))); !errors.Is(err, inbox.ErrFrozen) {
		t.Fatalf("frozen channel ingest err=%v, want ErrFrozen", err)
	}
	// Executed effect unchanged.
	if got := h.balance("alice"); got != 10 {
		t.Fatalf("alice=%d, effects of executed message must survive", got)
	}
	if got := h.balance("mallory"); got != 0 {
		t.Fatalf("mallory=%d, conflict copy must never execute", got)
	}
}

// TestReorgBeforeConfirmCancelsCandidates: a reorg replacing an unconfirmed
// block cancels its pending messages; after re-anchoring onto the new chain
// and confirming, they execute. Executed/final history is untouched.
func TestReorgBeforeConfirmCancelsCandidates(t *testing.T) {
	h := newHarness(t)
	const chain, ch = cryptoenvelope.ChainB, "reorg"

	// block 1 final with seq0 executed; block 2 proposed carrying seq1.
	h.propose(chain, 1)
	h.confirm(chain, 1)
	h.ingest(h.signMsg(chain, ch, 0, 1, transferBody("alice", 10)))

	h.propose(chain, 2)
	h.ingest(h.signMsg(chain, ch, 1, 2, transferBody("bob", 20)))
	if s := h.msgStatus(chain, ch, 1); s != "pending" {
		t.Fatalf("seq1 should be pending on proposed block, got %s", s)
	}

	// Reorg at height 2 (competing proposal) BEFORE confirmation.
	newHash := h.proposeCompeting(chain, 2, "fork")
	if s := h.msgStatus(chain, ch, 1); s != "cancelled" {
		t.Fatalf("seq1 should be cancelled after reorg, got %s", s)
	}
	// seq0 on the final height-1 block must survive.
	if s := h.msgStatus(chain, ch, 0); s != "executed" {
		t.Fatalf("seq0 status=%s want executed", s)
	}

	// Re-deliver seq1 anchored to the new block (resigned; anchor is in the
	// signed document, so this is a new valid signature from the source).
	h.ingest(h.signMsgHash(chain, ch, 1, newHash, transferBody("bob", 20)))
	if s := h.msgStatus(chain, ch, 1); s != "pending" {
		t.Fatalf("seq1 re-anchored status=%s want pending", s)
	}
	h.confirmHash(chain, newHash)
	if s := h.msgStatus(chain, ch, 1); s != "executed" {
		t.Fatalf("seq1 status=%s want executed after new chain confirmed", s)
	}
	if got := h.balance("bob"); got != 20 {
		t.Fatalf("bob=%d want 20", got)
	}
}

// TestFinalBlockConflictRefused: cannot replace an already-final height.
func TestFinalBlockConflictRefused(t *testing.T) {
	h := newHarness(t)
	const chain = cryptoenvelope.ChainA
	h.propose(chain, 1)
	h.confirm(chain, 1)
	hash := h.competingHash(chain, 1, "evil")
	err := h.svc.ProposeBlock(h.ctx, chain, hash, cryptoenvelope.ZeroHash, 1)
	if !errors.Is(err, inbox.ErrFinalConflict) {
		t.Fatalf("err=%v want ErrFinalConflict", err)
	}
}

// TestUnconfirmedNotExecutable: messages on proposed blocks never execute,
// even if the sequence is contiguous.
func TestUnconfirmedNotExecutable(t *testing.T) {
	h := newHarness(t)
	const chain, ch = cryptoenvelope.ChainA, "staging"
	h.propose(chain, 1) // intentionally not confirmed
	h.ingest(h.signMsg(chain, ch, 0, 1, transferBody("alice", 10)))
	if err := h.svc.AdvanceChannel(h.ctx, chain, ch); err != nil {
		t.Fatal(err)
	}
	if s := h.msgStatus(chain, ch, 0); s != "pending" {
		t.Fatalf("status=%s want pending (block not final)", s)
	}
	if got := h.balance("alice"); got != 0 {
		t.Fatalf("alice=%d, unconfirmed message must not apply effects", got)
	}
}

// TestInvalidSignatureRejected uses a real verification failure.
func TestInvalidSignatureRejected(t *testing.T) {
	h := newHarness(t)
	const chain, ch = cryptoenvelope.ChainA, "auth"
	h.propose(chain, 1)
	env := h.signMsg(chain, ch, 0, 1, transferBody("alice", 10))
	// Flip one signature byte.
	sig := []byte(env.Signature)
	if sig[len(sig)-1] == '0' {
		sig[len(sig)-1] = '1'
	} else {
		sig[len(sig)-1] = '0'
	}
	env.Signature = string(sig)
	if err := h.ingestErr(env); !errors.Is(err, inbox.ErrInvalidSignature) {
		t.Fatalf("err=%v want ErrInvalidSignature", err)
	}
}

// TestTwoChainsAreIndependent: chainA and chainB share no sequence/balance
// state even when they use the same channel id and sequence numbers.
func TestTwoChainsAreIndependent(t *testing.T) {
	h := newHarness(t)
	const ch = "shared-channel"
	for _, chain := range []string{cryptoenvelope.ChainA, cryptoenvelope.ChainB} {
		h.propose(chain, 1)
		h.confirm(chain, 1)
		h.ingest(h.signMsg(chain, ch, 0, 1, transferBody("treasury", 100)))
	}
	// Two separate executions of seq 0 on two chains credit the same target
	// twice; each chain has its own message row and execution ledger row.
	if got := h.balance("treasury"); got != 200 {
		t.Fatalf("treasury=%d want 200 (100 from each chain)", got)
	}
	for _, chain := range []string{cryptoenvelope.ChainA, cryptoenvelope.ChainB} {
		if s := h.msgStatus(chain, ch, 0); s != "executed" {
			t.Fatalf("%s seq0=%s want executed", chain, s)
		}
		executed, err := h.st.HasExecution(h.ctx, chain, ch, 0)
		if err != nil || !executed {
			t.Fatalf("%s execution ledger: executed=%v err=%v", chain, executed, err)
		}
	}
	// A signature produced by chainA's fixture must be rejected when relabeled
	// as chainB (signer pubkey swapped to chainB, signature stays chainA's).
	envA := h.signMsg(cryptoenvelope.ChainA, ch, 1, 1, transferBody("x", 1))
	envA.SourceChain = cryptoenvelope.ChainB
	envA.Signer = "0x" + hex.EncodeToString(h.keys[cryptoenvelope.ChainB].Public)
	if err := h.ingestErr(envA); !errors.Is(err, inbox.ErrInvalidSignature) {
		t.Fatalf("cross-chain forgery err=%v want ErrInvalidSignature", err)
	}
}

// TestInvalidBodyHaltsPrefix: an unprocessable message fails itself and
// blocks later sequences (no skipping over the prefix).
func TestInvalidBodyHaltsPrefix(t *testing.T) {
	h := newHarness(t)
	const chain, ch = cryptoenvelope.ChainA, "badbody"
	h.propose(chain, 1)
	h.confirm(chain, 1)
	h.ingest(h.signMsg(chain, ch, 0, 1, json.RawMessage(`{"type":"mint","amount":1}`)))
	h.ingest(h.signMsg(chain, ch, 1, 1, transferBody("alice", 10)))

	if s := h.msgStatus(chain, ch, 0); s != "execute_failed" {
		t.Fatalf("seq0=%s want execute_failed", s)
	}
	if s := h.msgStatus(chain, ch, 1); s != "pending" {
		t.Fatalf("seq1=%s must stay pending behind failed seq0", s)
	}
	alerts, _ := h.st.ListAlerts(h.ctx, chain, ch, 10)
	if len(alerts) != 1 || alerts[0].Kind != "execution_failed" {
		t.Fatalf("expected one execution_failed alert, got %v", alerts)
	}
}
