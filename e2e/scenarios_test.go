package e2e

import (
	"net/http"
	"testing"

	"inbox/internal/fixtures"
)

// TestHTTPScenarios drives the public HTTP API end to end: gap fill with
// out-of-order staging, identical-duplicate idempotency, a conflicting copy
// after execution (freeze + alert), and a pre-finality reorg.
func TestHTTPScenarios(t *testing.T) {
	bin := buildServer(t)

	t.Run("gap_fill_and_idempotent_duplicate", func(t *testing.T) {
		s := startServer(t, bin, "")
		cf := fixtures.Chains[fixtures.ChainA]
		alice := cf.Senders[0]
		b := fixtures.NewChainBuilder(cf)
		blk1 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "orders", Nonce: 1, Sender: alice, Payload: []byte("one")}})
		blk2 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "orders", Nonce: 2, Sender: alice, Payload: []byte("two")}})
		blk3 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "orders", Nonce: 3, Sender: alice, Payload: []byte("three")}})
		b4, b5 := b.EmptyBlock(), b.EmptyBlock()

		mustPost := func(path string, body any, want int) {
			t.Helper()
			if code, resp := s.post(path, body); code != want {
				t.Fatalf("%s -> %d (want %d): %v", path, code, want, resp)
			}
		}
		mustPost("/v1/chains/chain-a/channels", openDTO(cf.ID, "orders", alice.Pub), http.StatusCreated)
		for _, h := range []fixtures.BuiltBlock{blk1, blk2, blk3} {
			mustPost("/v1/headers", headerDTO(h), http.StatusAccepted)
		}
		// Submit 1 then 3 (3 is ahead of the watermark and unconfirmed).
		mustPost("/v1/messages", messageDTO(blk1.Messages[0]), http.StatusAccepted)
		mustPost("/v1/messages", messageDTO(blk3.Messages[0]), http.StatusAccepted)
		if code, body := s.post("/v1/process", map[string]any{}); code != 200 || int(body["delivered"].(float64)) != 1 {
			t.Fatalf("tick1 code=%d body=%v", code, body)
		}
		// Gap fill: nonce 2 arrives, block2 still unconfirmed -> no delivery.
		mustPost("/v1/messages", messageDTO(blk2.Messages[0]), http.StatusAccepted)
		if _, body := s.post("/v1/process", map[string]any{}); int(body["delivered"].(float64)) != 0 {
			t.Fatalf("unconfirmed nonce2 delivered: %v", body)
		}
		mustPost("/v1/headers", headerDTO(b4), http.StatusAccepted)
		if _, body := s.post("/v1/process", map[string]any{}); int(body["delivered"].(float64)) != 1 {
			t.Fatalf("nonce2 not delivered after block4: %v", body)
		}
		mustPost("/v1/headers", headerDTO(b5), http.StatusAccepted)
		if _, body := s.post("/v1/process", map[string]any{}); int(body["delivered"].(float64)) != 1 {
			t.Fatalf("nonce3 not delivered after block5: %v", body)
		}
		// Identical duplicate is idempotent and does not re-execute.
		mustPost("/v1/messages", messageDTO(blk1.Messages[0]), http.StatusAccepted)
		if _, body := s.post("/v1/process", map[string]any{}); int(body["delivered"].(float64)) != 0 {
			t.Fatalf("duplicate re-executed: %v", body)
		}
		expectDeliveries(t, s, []int{1, 2, 3})
	})

	t.Run("conflict_after_execution_freezes", func(t *testing.T) {
		s := startServer(t, bin, "")
		cf := fixtures.Chains[fixtures.ChainA]
		bob := cf.Senders[1]
		b := fixtures.NewChainBuilder(cf)
		blk1 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "settle", Nonce: 1, Sender: bob, Payload: []byte("body-A")}})
		b2 := b.BuildBlock(nil)
		blk3 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "settle", Nonce: 1, Sender: bob, Payload: []byte("body-B")}})

		mustPost := func(path string, body any, want int) {
			t.Helper()
			if code, resp := s.post(path, body); code != want {
				t.Fatalf("%s -> %d (want %d): %v", path, code, want, resp)
			}
		}
		mustPost("/v1/chains/chain-a/channels", openDTO(cf.ID, "settle", bob.Pub), http.StatusCreated)
		mustPost("/v1/headers", headerDTO(blk1), http.StatusAccepted)
		mustPost("/v1/headers", headerDTO(b2), http.StatusAccepted)
		mustPost("/v1/messages", messageDTO(blk1.Messages[0]), http.StatusAccepted)
		mustPost("/v1/headers", headerDTO(blk3), http.StatusAccepted)
		if _, body := s.post("/v1/process", map[string]any{}); int(body["delivered"].(float64)) != 1 {
			t.Fatalf("body A should execute: %v", body)
		}
		// Conflicting body for the already-executed key -> 409 + frozen.
		if code, body := s.post("/v1/messages", messageDTO(blk3.Messages[0])); code != http.StatusConflict {
			t.Fatalf("conflict code=%d body=%v", code, body)
		} else if body["code"] != "message_conflict_channel_frozen" {
			t.Fatalf("unexpected conflict code: %v", body)
		}
		// Channel frozen -> subsequent messages locked out.
		if code, _ := s.post("/v1/messages", messageDTO(blk1.Messages[0])); code != http.StatusLocked {
			t.Fatalf("frozen channel accepted a message (code=%d)", code)
		}
		// A critical alert exists.
		if code, alerts := s.get("/v1/alerts?chain_id=chain-a&severity=critical"); code != 200 {
			t.Fatalf("alerts code=%d", code)
		} else {
			found := false
			for _, a := range alerts {
				if a["kind"] == "message_conflict_executed" {
					found = true
				}
			}
			if !found {
				t.Fatalf("missing message_conflict_executed alert: %v", alerts)
			}
		}
	})

	t.Run("pre_finality_reorg_cancels_then_replaces", func(t *testing.T) {
		s := startServer(t, bin, "")
		cf := fixtures.Chains[fixtures.ChainB]
		dave := cf.Senders[0]
		b := fixtures.NewChainBuilder(cf)
		blk1 := b.BuildBlock(nil)
		tip2 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "payments", Nonce: 1, Sender: dave, Payload: []byte("old")}})

		if code, _ := s.post("/v1/chains/chain-b/channels", openDTO(cf.ID, "payments", dave.Pub)); code != http.StatusCreated {
			t.Fatal("open channel")
		}
		if code, _ := s.post("/v1/headers", headerDTO(blk1)); code != http.StatusAccepted {
			t.Fatal("h1")
		}
		if code, _ := s.post("/v1/headers", headerDTO(tip2)); code != http.StatusAccepted {
			t.Fatal("h2")
		}
		if code, _ := s.post("/v1/messages", messageDTO(tip2.Messages[0])); code != http.StatusAccepted {
			t.Fatal("old msg")
		}
		pub, sig := b.Revocation(tip2.Block)
		if code, body := s.post("/v1/revocations", map[string]any{
			"chain_id": cf.ID, "block_hex": hexBytes(tip2.Block[:]),
			"validator_pub_hex": hexBytes(pub), "signature_hex": hexBytes(sig),
		}); code != http.StatusOK {
			t.Fatalf("revoke code=%d body=%v", code, body)
		}
		// Old message is now anchored at a non-canonical block.
		if code, _ := s.post("/v1/messages", messageDTO(tip2.Messages[0])); code != http.StatusUnprocessableEntity {
			t.Fatalf("expected 422 for revoked-block message, got %d", code)
		}
		fork := fixtures.NewForkBuilder(cf, blk1.Block, blk1.Height)
		newBlk2 := fork.BuildBlock([]fixtures.MsgSpec{{ChannelID: "payments", Nonce: 1, Sender: dave, Payload: []byte("new")}})
		cont := fixtures.NewForkBuilder(cf, newBlk2.Block, newBlk2.Height)
		blk3 := cont.BuildBlock(nil)
		blk4 := fixtures.NewForkBuilder(cf, blk3.Block, blk3.Height).BuildBlock(nil)
		if code, _ := s.post("/v1/headers", headerDTO(newBlk2)); code != http.StatusAccepted {
			t.Fatal("new h2")
		}
		if code, _ := s.post("/v1/messages", messageDTO(newBlk2.Messages[0])); code != http.StatusAccepted {
			t.Fatal("new msg")
		}
		if code, _ := s.post("/v1/headers", headerDTO(blk3)); code != http.StatusAccepted {
			t.Fatal("h3")
		}
		if code, _ := s.post("/v1/headers", headerDTO(blk4)); code != http.StatusAccepted {
			t.Fatal("h4")
		}
		if _, body := s.post("/v1/process", map[string]any{}); int(body["delivered"].(float64)) != 1 {
			t.Fatalf("replacement nonce1 should deliver once: %v", body)
		}
		code, rows := s.get("/v1/chains/chain-b/channels/payments/deliveries")
		if code != 200 || len(rows) != 1 || int(rows[0]["nonce"].(float64)) != 1 ||
			rows[0]["block_hex"] != hexBytes(newBlk2.Block[:]) {
			t.Fatalf("delivery must reference replacement block: %d %v", code, rows)
		}
	})
}
