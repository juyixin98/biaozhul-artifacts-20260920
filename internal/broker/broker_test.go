package broker

import (
	"testing"
	"time"
)

func newTestBroker(t *testing.T, maxEvents, queue int) *Broker {
	t.Helper()
	b, err := Open(Config{Dir: t.TempDir(), MaxEvents: maxEvents, QueueSize: queue, NoSync: true})
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = b.Close() })
	return b
}

func TestPublishMonotonicAndReplay(t *testing.T) {
	b := newTestBroker(t, 0, 8)
	for i := 0; i < 5; i++ {
		e, err := b.Publish("msg", "payload")
		if err != nil {
			t.Fatalf("Publish: %v", err)
		}
		if e.ID != int64(i+1) {
			t.Fatalf("id = %d, want %d", e.ID, i+1)
		}
		if e.Event != "msg" || e.Data != "payload" {
			t.Fatalf("event content mismatch: %+v", e)
		}
		if e.Timestamp.IsZero() {
			t.Fatal("timestamp not set")
		}
	}

	cases := []struct {
		after    int64
		wantFrom int64
		wantN    int
	}{
		{0, 1, 5}, // full backlog
		{2, 3, 3}, // strictly after cursor
		{5, 6, 0}, // caught up: nothing
	}
	for _, tc := range cases {
		got, oldest, last, ok, ahead := b.Replay(tc.after)
		if !ok || ahead {
			t.Fatalf("Replay(%d) ok=%v ahead=%v", tc.after, ok, ahead)
		}
		if oldest != 1 || last != 5 {
			t.Fatalf("Replay(%d) oldest=%d last=%d, want 1,5", tc.after, oldest, last)
		}
		if len(got) != tc.wantN {
			t.Fatalf("Replay(%d) returned %d events, want %d", tc.after, len(got), tc.wantN)
		}
		if tc.wantN > 0 && got[0].ID != tc.wantFrom {
			t.Fatalf("Replay(%d) first id=%d, want %d", tc.after, got[0].ID, tc.wantFrom)
		}
	}
}

func TestPersistenceAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	b1, err := Open(Config{Dir: dir, MaxEvents: 0, NoSync: true})
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	for i := 0; i < 3; i++ {
		if _, err := b1.Publish("evt", "d"); err != nil {
			t.Fatalf("Publish: %v", err)
		}
	}
	if err := b1.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	b2, err := Open(Config{Dir: dir, MaxEvents: 0, NoSync: true})
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	st := b2.Stats()
	if st.LastID != 3 || st.Retained != 3 || st.OldestID != 1 {
		t.Fatalf("after reopen stats = %+v", st)
	}
	got, _, _, ok, _ := b2.Replay(1)
	if !ok || len(got) != 2 || got[0].ID != 2 || got[1].ID != 3 {
		t.Fatalf("after reopen replay = %+v ok=%v", ids(got), ok)
	}
	// IDs continue monotonically from the persisted head.
	e, err := b2.Publish("evt", "d")
	if err != nil {
		t.Fatalf("Publish after reopen: %v", err)
	}
	if e.ID != 4 {
		t.Fatalf("post-restart id = %d, want 4", e.ID)
	}
}

func TestCorruptLogRejected(t *testing.T) {
	dir := t.TempDir()
	writeLog(t, dir, "{\"id\":1,\"data\":\"a\"}\nnot-json\n")
	if _, err := Open(Config{Dir: dir, NoSync: true}); err == nil {
		t.Fatal("expected error opening corrupt log")
	}

	dir2 := t.TempDir()
	writeLog(t, dir2, "{\"id\":1,\"data\":\"a\"}\n{\"id\":3,\"data\":\"c\"}\n")
	if _, err := Open(Config{Dir: dir2, NoSync: true}); err == nil {
		t.Fatal("expected error opening log with id gap")
	}
}

func TestRetentionCompaction(t *testing.T) {
	b := newTestBroker(t, 3, 4)
	for i := 0; i < 7; i++ {
		if _, err := b.Publish("m", "x"); err != nil {
			t.Fatalf("Publish: %v", err)
		}
	}
	st := b.Stats()
	if st.Retained != 3 || st.OldestID != 5 || st.LastID != 7 {
		t.Fatalf("stats = %+v, want retained=3 oldest=5 last=7", st)
	}

	// Cursor classification around the window edge:
	// oldest=5; cursor 4 (oldest-1) is still resumable (replays 5..7),
	// cursor 3 means an expired event may be missing.
	if _, _, s := b.CheckCursor(3); s != CursorExpired {
		t.Fatalf("cursor 3: %v, want Expired", s)
	}
	if _, _, s := b.CheckCursor(4); s != CursorOK {
		t.Fatalf("cursor 4: %v, want OK (oldest-1 replays the retained window)", s)
	}
	if got, _, _, ok, _ := b.Replay(4); !ok || len(got) != 3 || got[0].ID != 5 {
		t.Fatalf("Replay(4) = %+v ok=%v, want 5,6,7", ids(got), ok)
	}
	if _, _, s := b.CheckCursor(5); s != CursorOK {
		t.Fatalf("cursor 5: %v, want OK", s)
	}
	if _, _, s := b.CheckCursor(7); s != CursorOK {
		t.Fatalf("cursor 7: %v, want OK", s)
	}
	if _, _, s := b.CheckCursor(8); s != CursorAhead {
		t.Fatalf("cursor 8: %v, want Ahead", s)
	}

	// Survives restart: compacted log on disk contains only 5,6,7.
	if err := b.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	b2, err := Open(Config{Dir: b.dir, MaxEvents: 3, NoSync: true})
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	defer b2.Close()
	got, _, _, ok, _ := b2.Replay(0)
	if !ok || len(got) != 3 || got[0].ID != 5 {
		t.Fatalf("after reopen events = %+v", ids(got))
	}
	// A previously valid cursor (2) is now expired after restart.
	if _, _, s := b2.CheckCursor(2); s != CursorExpired {
		t.Fatalf("old cursor after restart: %v, want Expired", s)
	}
}

func TestRetentionTightenedOnRestart(t *testing.T) {
	dir := t.TempDir()
	b1, err := Open(Config{Dir: dir, MaxEvents: 10, NoSync: true})
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	for i := 0; i < 10; i++ {
		_, _ = b1.Publish("m", "x")
	}
	_ = b1.Close()

	b2, err := Open(Config{Dir: dir, MaxEvents: 4, NoSync: true})
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	defer b2.Close()
	st := b2.Stats()
	if st.Retained != 4 || st.OldestID != 7 {
		t.Fatalf("stats = %+v, want retained=4 oldest=7", st)
	}
}

func TestSubscribeDeliversLiveAndDropSlow(t *testing.T) {
	b := newTestBroker(t, 0, 2)
	sub, head := b.Subscribe()
	if head != 0 {
		t.Fatalf("head on empty log = %d, want 0", head)
	}
	defer b.Unsubscribe(sub)

	// No goroutine drains the channel. Sends are non-blocking: the first two
	// fill the 2-slot buffer; the third overflows and evicts the subscriber.
	if e, err := b.Publish("m", "1"); err != nil || e.ID != 1 {
		t.Fatalf("publish 1: e=%+v err=%v", e, err)
	}
	if e, err := b.Publish("m", "2"); err != nil || e.ID != 2 {
		t.Fatalf("publish 2: e=%+v err=%v", e, err)
	}
	if e, err := b.Publish("m", "3"); err != nil || e.ID != 3 {
		t.Fatalf("publish 3: e=%+v err=%v", e, err)
	}

	// Channel is closed on eviction; the buffered events 1,2 remain readable.
	got := []int64{}
	for ev := range sub.C {
		got = append(got, ev.ID)
	} // channel closed on eviction
	if len(got) != 2 || got[0] != 1 || got[1] != 2 {
		t.Fatalf("received %v before eviction, want [1 2]", got)
	}
	if b.Stats().Dropped != 1 {
		t.Fatalf("dropped = %d, want 1", b.Stats().Dropped)
	}
}

func TestSubscribeHeadSeamNoGap(t *testing.T) {
	// The core invariant: subscribe first, replay (cursor, head] afterwards;
	// anything live during replay has id > head => no gap, possible dup only.
	b := newTestBroker(t, 0, 16)
	for i := 0; i < 10; i++ {
		_, _ = b.Publish("m", "x")
	}
	sub, head := b.Subscribe()
	defer b.Unsubscribe(sub)
	got, _, _, ok, _ := b.Replay(0)
	if !ok {
		t.Fatal("replay failed")
	}
	if len(got) != 10 || got[len(got)-1].ID != head {
		t.Fatalf("replay does not cover head: %d events, head=%d", len(got), head)
	}

	// Publish concurrently-ish: one live event; it must be exactly head+1.
	go func() { _, _ = b.Publish("m", "live") }()
	select {
	case e := <-sub.C:
		if e.ID != head+1 {
			t.Fatalf("live id = %d, want head+1 = %d", e.ID, head+1)
		}
	case <-time.After(time.Second):
		t.Fatal("live event not delivered")
	}
}

func TestUnsubscribeStopsDelivery(t *testing.T) {
	b := newTestBroker(t, 0, 4)
	sub, _ := b.Subscribe()
	b.Unsubscribe(sub)
	if _, err := b.Publish("m", "x"); err != nil {
		t.Fatalf("Publish: %v", err)
	}
	// Double unsubscribe is safe.
	b.Unsubscribe(sub)
	if b.Stats().Subscribers != 0 {
		t.Fatalf("subscribers = %d, want 0", b.Stats().Subscribers)
	}
}

func ids(es []Event) []int64 {
	out := make([]int64, len(es))
	for i, e := range es {
		out[i] = e.ID
	}
	return out
}
