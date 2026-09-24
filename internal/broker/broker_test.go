package broker

import (
	"context"
	"io"
	"log"
	"path/filepath"
	"testing"
	"time"

	"sseserver/internal/store"
)

func newTestBroker(t *testing.T, maxEvents, buffer int) (*Broker, *store.Store) {
	t.Helper()
	st, err := store.Open(store.Options{
		Dir:               filepath.Join(t.TempDir(), "data"),
		MaxEvents:         maxEvents,
		CompactEventDelta: 1,
		Sync:              true,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	b := New(st, Config{BufferSize: buffer}, log.New(io.Discard, "", 0))
	return b, st
}

func publishN(t *testing.T, b *Broker, n int) {
	t.Helper()
	for i := 0; i < n; i++ {
		if _, err := b.Publish(context.Background(), "e"); err != nil {
			t.Fatal(err)
		}
	}
}

func idsOf(evs []store.Event) []uint64 {
	out := make([]uint64, len(evs))
	for i, ev := range evs {
		out[i] = ev.ID
	}
	return out
}

// TestReplayWithConcurrentPublishNoDups is the core seam test: events
// published while the handler is still writing the replay snapshot must land
// in pending, strictly after the replayed IDs, with neither gaps nor
// duplicates.
func TestReplayWithConcurrentPublishNoDups(t *testing.T) {
	b, _ := newTestBroker(t, 0, 1024)
	publishN(t, b, 100)

	res := b.Subscribe(50, true) // replay 51..100
	if got := idsOf(res.Replay); len(got) != 50 || got[0] != 51 || got[49] != 100 {
		t.Fatalf("replay window wrong: %v", got)
	}

	// Handler still draining replay: new events must go to pending.
	publishN(t, b, 50) // IDs 101..150

	var ids []uint64
	ids = append(ids, idsOf(res.Replay)...)
	pending, ok := b.Activate(res.Sub)
	if !ok {
		t.Fatal("subscriber dropped unexpectedly")
	}
	ids = append(ids, idsOf(pending)...)
	b.Unsubscribe(res.Sub)

	if len(ids) != 100 {
		t.Fatalf("collected %d events want 100", len(ids))
	}
	seen := map[uint64]bool{}
	for i, id := range ids {
		if want := uint64(51 + i); id != want {
			t.Fatalf("position %d: id=%d want %d (gap/dup at seam)", i, id, want)
		}
		if seen[id] {
			t.Fatalf("duplicate id %d", id)
		}
		seen[id] = true
	}
}

// TestPendingEventsPublishedBeforeActivation verifies that a subscriber which
// activates only after a burst still receives every event exactly once.
func TestPendingEventsBeforeActivation(t *testing.T) {
	b, _ := newTestBroker(t, 0, 128)
	publishN(t, b, 10)

	res := b.Subscribe(10, true) // empty replay, live-only cursor
	if len(res.Replay) != 0 {
		t.Fatalf("replay should be empty, got %d", len(res.Replay))
	}
	publishN(t, b, 20) // IDs 11..30 arrive before Activate

	pending, ok := b.Activate(res.Sub)
	if !ok {
		t.Fatal("dropped during pending")
	}
	if got := idsOf(pending); len(got) != 20 || got[0] != 11 || got[19] != 30 {
		t.Fatalf("pending wrong: %v", got)
	}
	b.Unsubscribe(res.Sub)
}

// TestLiveDeliveryAfterActivation checks post-activation channel delivery.
func TestLiveDeliveryAfterActivation(t *testing.T) {
	b, _ := newTestBroker(t, 0, 8)
	publishN(t, b, 3)
	res := b.Subscribe(3, true)
	if _, ok := b.Activate(res.Sub); !ok {
		t.Fatal("activate failed")
	}
	publishN(t, b, 2) // IDs 4,5

	for want := uint64(4); want <= 5; want++ {
		select {
		case ev := <-res.Sub.Items():
			if ev.ID != want {
				t.Fatalf("got id %d want %d", ev.ID, want)
			}
		case <-time.After(time.Second):
			t.Fatalf("timeout waiting for id %d", want)
		}
	}
	b.Unsubscribe(res.Sub)
}

func TestExpiredCursorReset(t *testing.T) {
	b, st := newTestBroker(t, 10, 64)
	publishN(t, b, 30) // retained window 21..30
	bounds := st.Bounds()

	res := b.Subscribe(5, true) // 5+1 < 21 -> expired
	if res.Reset == nil {
		t.Fatal("expected reset")
	}
	if res.Reset.Reason != "expired" || res.Reset.Oldest != bounds.Oldest || res.Reset.Last != 30 {
		t.Fatalf("reset = %+v", res.Reset)
	}
	if res.Sub != nil {
		t.Fatal("no subscriber may be registered on reset")
	}
	if n := b.SubscriberCount(); n != 0 {
		t.Fatalf("subscriber count after reset = %d", n)
	}

	// oldest-1 is a valid "replay the whole retained window" cursor.
	res2 := b.Subscribe(bounds.Oldest-1, true)
	if res2.Reset != nil || len(res2.Replay) != 10 {
		t.Fatalf("oldest-1 cursor should replay window, reset=%v replay=%d",
			res2.Reset, len(res2.Replay))
	}
	b.Unsubscribe(res2.Sub)
}

func TestFutureCursorReset(t *testing.T) {
	b, _ := newTestBroker(t, 0, 64)
	publishN(t, b, 3)
	res := b.Subscribe(999, true)
	if res.Reset == nil || res.Reset.Reason != "future" {
		t.Fatalf("expected future reset, got %+v", res.Reset)
	}
}

func TestSlowConsumerLiveDropped(t *testing.T) {
	b, _ := newTestBroker(t, 0, 4)
	publishN(t, b, 1)
	res := b.Subscribe(1, true)
	if _, ok := b.Activate(res.Sub); !ok {
		t.Fatal("activate failed")
	}

	// Never read Items(): publishes must not block and the subscriber must
	// be force-disconnected once its buffer stays full.
	done := make(chan struct{})
	go func() {
		publishN(t, b, 20)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Publish blocked on a slow consumer")
	}
	select {
	case <-res.Sub.Done():
	case <-time.After(time.Second):
		t.Fatal("slow subscriber was not dropped")
	}
	if res.Sub.DropReason() != "slow_consumer" {
		t.Fatalf("drop reason = %q", res.Sub.DropReason())
	}
	if b.SlowDrops() != 1 {
		t.Fatalf("slow drops = %d", b.SlowDrops())
	}
	if b.SubscriberCount() != 0 {
		t.Fatalf("dropped subscriber still registered: %d", b.SubscriberCount())
	}
}

func TestSlowConsumerDuringReplayDropped(t *testing.T) {
	b, _ := newTestBroker(t, 0, 4)
	publishN(t, b, 50)
	res := b.Subscribe(0, true) // 50 events to replay, subscriber not active yet
	// Flood beyond the pending cap while replay is still draining.
	publishN(t, b, 20)
	select {
	case <-res.Sub.Done():
	case <-time.After(time.Second):
		t.Fatal("replay-phase subscriber not dropped on pending overflow")
	}
	if _, ok := b.Activate(res.Sub); ok {
		t.Fatal("activate must report a dropped subscriber")
	}
}

func TestUnsubscribeIsIdempotent(t *testing.T) {
	b, _ := newTestBroker(t, 0, 4)
	res := b.Subscribe(0, false)
	b.Unsubscribe(res.Sub)
	b.Unsubscribe(res.Sub) // must not panic
	if b.SubscriberCount() != 0 {
		t.Fatal("count not zero")
	}
}
