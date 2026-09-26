package ledger

import (
	"testing"
	"time"
)

func TestLifecycle(t *testing.T) {
	l := New()
	t0 := time.Unix(100, 0)
	id1 := l.Begin("/a", "request", t0)
	id2 := l.Begin("/b", "background-task", t0)
	if id1 != 1 || id2 != 2 {
		t.Fatalf("ids = %d,%d, want 1,2", id1, id2)
	}
	l.End(id1, t0.Add(time.Second), OutcomeCompleted, "")
	l.End(id2, t0.Add(2*time.Second), OutcomeCancelled, "context canceled")

	got := l.Snapshot()
	if len(got) != 2 {
		t.Fatalf("entries = %d, want 2", len(got))
	}
	if got[0].Outcome != OutcomeCompleted || got[0].EndedAt != t0.Add(time.Second) {
		t.Errorf("entry1 = %+v", got[0])
	}
	if got[1].Outcome != OutcomeCancelled || got[1].Detail != "context canceled" {
		t.Errorf("entry2 = %+v", got[1])
	}
	if got[1].Kind != "background-task" {
		t.Errorf("kind = %q", got[1].Kind)
	}

	// Snapshot must be a defensive copy: mutating it must not affect the ledger.
	got[0].Outcome = OutcomeCancelled
	if l.Snapshot()[0].Outcome != OutcomeCompleted {
		t.Fatal("Snapshot is not a defensive copy")
	}
}

func TestEndUnknownIDIsSafe(t *testing.T) {
	l := New()
	l.End(999, time.Now(), OutcomeCompleted, "") // must not panic
	if len(l.Snapshot()) != 0 {
		t.Fatal("unknown End must not create an entry")
	}
}
