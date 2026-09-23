package core_test

import (
	"context"
	"testing"

	"inbox/internal/fixtures"
)

// TestCrashAtomicityInProcess simulates a hard crash at both well-defined
// points by panicking inside the delivery transaction. The deferred
// rollback must undo every staged side effect; reprocessing must then
// deliver each nonce exactly once.
func TestCrashAtomicityInProcess(t *testing.T) {
	for _, point := range []string{"before_deliver", "before_commit"} {
		t.Run(point, func(t *testing.T) {
			h := newHarness(t)
			ex := h.Exec
			ctx := context.Background()
			cf := fixtures.Chains[fixtures.ChainA]
			alice := cf.Senders[0]
			if err := ex.OpenChannel(ctx, cf.ID, "crash", []key{alice.Pub}); err != nil {
				t.Fatal(err)
			}
			b := fixtures.NewChainBuilder(cf)
			blk1 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "crash", Nonce: 1, Sender: alice, Payload: []byte("one")}})
			blk2 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "crash", Nonce: 2, Sender: alice, Payload: []byte("two")}})
			blk3 := b.BuildBlock([]fixtures.MsgSpec{{ChannelID: "crash", Nonce: 3, Sender: alice, Payload: []byte("three")}})
			blk4 := b.EmptyBlock()
			blk5 := b.EmptyBlock()
			for _, blk := range []fixtures.BuiltBlock{blk1, blk2, blk3, blk4, blk5} {
				submitBlock(t, ex, blk)
			}
			for _, blk := range []fixtures.BuiltBlock{blk1, blk2, blk3} {
				submitMsg(t, ex, blk.Messages[0])
			}

			// Panic exactly once, on nonce 1's delivery, at the requested
			// crash point.
			fired := false
			ex.SetCrashHook(func(p, _, _ string, nonce uint64) {
				if p == point && nonce == 1 && !fired {
					fired = true
					panic("simulated hard crash: " + p)
				}
			})

			runOnceExpectPanic := func() {
				defer func() {
					if r := recover(); r == nil {
						t.Fatalf("expected panic at %s", point)
					}
				}()
				_, _ = ex.ProcessArmed(ctx)
			}
			runOnceExpectPanic()

			if !fired {
				t.Fatalf("crash hook %s never fired", point)
			}

			// Remove the hook and reprocess: nothing partial may remain.
			ex.SetCrashHook(nil)
			n, err := ex.ProcessArmed(ctx)
			if err != nil {
				t.Fatal(err)
			}
			if n != 3 {
				t.Fatalf("after in-process crash, deliveries=%d, want 3", n)
			}
			got := deliveriesOf(t, ex, cf.ID, "crash")
			if len(got) != 3 {
				t.Fatalf("deliveries=%d, want 3", len(got))
			}
			for i, d := range got {
				if d.Nonce != uint64(i+1) || d.Attempts != 1 {
					t.Fatalf("delivery %d: %+v (want in-order, attempts=1)", i, d)
				}
			}
		})
	}
}
