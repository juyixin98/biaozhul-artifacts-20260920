package e2e

import (
	"net/http"
	"testing"

	"inbox/internal/fixtures"
)

// seedCrashScenario opens a channel and builds five blocks on chain-a so
// that nonces 1 and 2 are confirmed (confirmation depth 2). Messages are
// submitted over HTTP.
func seedCrashScenario(t *testing.T, s *server) {
	t.Helper()
	cf := fixtures.Chains[fixtures.ChainA]
	alice := cf.Senders[0]
	b := fixtures.NewChainBuilder(cf)
	blk1 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "orders", Nonce: 1, Sender: alice, Payload: []byte("one")}})
	blk2 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "orders", Nonce: 2, Sender: alice, Payload: []byte("two")}})
	blk3 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "orders", Nonce: 3, Sender: alice, Payload: []byte("three")}})
	b4, b5 := b.EmptyBlock(), b.EmptyBlock()

	if code, _ := s.post("/v1/chains/chain-a/channels", openDTO(cf.ID, "orders", alice.Pub)); code != http.StatusCreated {
		t.Fatalf("open channel: %d", code)
	}
	for _, h := range []fixtures.BuiltBlock{blk1, blk2, blk3, b4, b5} {
		if code, body := s.post("/v1/headers", headerDTO(h)); code != http.StatusAccepted {
			t.Fatalf("submit header h%d: %d %v", h.Height, code, body)
		}
	}
	for _, m := range []fixtures.BuiltMessage{blk1.Messages[0], blk2.Messages[0], blk3.Messages[0]} {
		if code, body := s.post("/v1/messages", messageDTO(m)); code != http.StatusAccepted {
			t.Fatalf("submit nonce %d: %d %v", m.Nonce, code, body)
		}
	}
}

func expectDeliveries(t *testing.T, s *server, wantNonces []int) {
	t.Helper()
	code, rows := s.get("/v1/chains/chain-a/channels/orders/deliveries")
	if code != 200 {
		t.Fatalf("list deliveries: %d", code)
	}
	if len(rows) != len(wantNonces) {
		t.Fatalf("deliveries=%v, want nonces %v", rows, wantNonces)
	}
	for i, n := range wantNonces {
		got := int(rows[i]["nonce"].(float64))
		if got != n {
			t.Fatalf("delivery %d nonce=%d, want %d", i, got, n)
		}
		if att := int(rows[i]["attempts"].(float64)); att != 1 {
			t.Fatalf("nonce %d attempts=%d, want 1 (exactly-once across hard crash)", n, att)
		}
	}
}

// TestCrashBeforeCommitThenRestart crashes the REAL process inside the
// delivery transaction (after all side effects are staged, before COMMIT),
// then restarts it against the same database and asserts every nonce is
// delivered exactly once.
func TestCrashBeforeCommitThenRestart(t *testing.T) {
	bin := buildServer(t)
	s := startServer(t, bin, "before_commit@1")
	seedCrashScenario(t, s)

	// Nothing delivered before the crash.
	expectDeliveries(t, s, nil)

	// /process hard-crashes the process with exit 99 while delivering nonce1.
	s.crashAndExpectExit(99)

	// Restart WITHOUT the crash hook; durable state must show zero partial
	// deliveries (the pre-commit transaction was rolled back).
	s2 := s.restart("")
	expectDeliveries(t, s2, nil)

	if code, body := s2.post("/v1/process", map[string]any{}); code != 200 || int(body["delivered"].(float64)) != 3 {
		t.Fatalf("post-restart process: code=%d body=%v, want 3 deliveries", code, body)
	}
	expectDeliveries(t, s2, []int{1, 2, 3})

	// A second processing run is a no-op: no double execution.
	if code, body := s2.post("/v1/process", map[string]any{}); code != 200 || int(body["delivered"].(float64)) != 0 {
		t.Fatalf("idempotent reprocess: code=%d body=%v, want 0", code, body)
	}
	expectDeliveries(t, s2, []int{1, 2, 3})
}

// TestCrashBeforeDeliverThenRestart crashes before any write, restart and
// deliver once.
func TestCrashBeforeDeliverThenRestart(t *testing.T) {
	bin := buildServer(t)
	s := startServer(t, bin, "before_deliver@1")
	seedCrashScenario(t, s)
	s.crashAndExpectExit(99)

	s2 := s.restart("")
	expectDeliveries(t, s2, nil)
	if code, body := s2.post("/v1/process", map[string]any{}); code != 200 || int(body["delivered"].(float64)) != 3 {
		t.Fatalf("post-restart process: code=%d body=%v, want 3", code, body)
	}
	expectDeliveries(t, s2, []int{1, 2, 3})
}

// TestRestartResumesExistingProgress verifies a normal restart after some
// deliveries resumes from the persisted watermark without re-executing.
func TestRestartResumesExistingProgress(t *testing.T) {
	bin := buildServer(t)
	s := startServer(t, bin, "")
	seedCrashScenario(t, s)
	if _, body := s.post("/v1/process", map[string]any{}); int(body["delivered"].(float64)) != 3 {
		t.Fatalf("initial process body=%v", body)
	}
	// Graceful stop, then restart on the same schema.
	s.stop()
	s2 := s.restart("")
	if code, body := s2.post("/v1/process", map[string]any{}); code != 200 || int(body["delivered"].(float64)) != 0 {
		t.Fatalf("restart re-executed: code=%d body=%v", code, body)
	}
	expectDeliveries(t, s2, []int{1, 2, 3})
}
