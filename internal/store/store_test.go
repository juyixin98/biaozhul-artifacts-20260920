package store

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/local/testmerge/internal/domain"
)

func TestAppendAndReload(t *testing.T) {
	dir := t.TempDir()
	st, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := st.CreateRun("run-1"); err != nil {
		t.Fatal(err)
	}
	e1 := domain.Event{EventID: "e1", SeqNo: 1, Type: domain.EventShardStarted, RunID: "run-1", Shard: "a"}
	e2 := domain.Event{EventID: "e2", SeqNo: 2, Type: domain.EventAttemptFinished, RunID: "run-1",
		Shard: "a", TestID: "t1", AttemptID: "a1", AttemptNo: 1, Status: "passed"}
	if err := st.AppendEvent(e1); err != nil {
		t.Fatal(err)
	}
	if err := st.AppendEvent(e2); err != nil {
		t.Fatal(err)
	}

	// Reopen from disk: durability.
	st2, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	got, err := st2.LoadEvents("run-1")
	if err != nil || len(got) != 2 {
		t.Fatalf("reload: %v events=%d", err, len(got))
	}
}

func TestDuplicateEventIDRejected(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir)
	if err := st.CreateRun("r"); err != nil {
		t.Fatal(err)
	}
	e := domain.Event{EventID: "e1", SeqNo: 1, Type: domain.EventShardStarted, RunID: "r", Shard: "a"}
	if err := st.AppendEvent(e); err != nil {
		t.Fatal(err)
	}
	err := st.AppendEvent(e)
	var dup *DuplicateEventError
	if !errors.As(err, &dup) {
		t.Fatalf("want DuplicateEventError, got %v", err)
	}
}

func TestSeqCollisionRejected(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir)
	_ = st.CreateRun("r")
	e1 := domain.Event{EventID: "e1", SeqNo: 5, Type: domain.EventShardStarted, RunID: "r", Shard: "a"}
	e2 := domain.Event{EventID: "e2", SeqNo: 5, Type: domain.EventShardStarted, RunID: "r", Shard: "b"}
	if err := st.AppendEvent(e1); err != nil {
		t.Fatal(err)
	}
	err := st.AppendEvent(e2)
	var col *SeqCollisionError
	if !errors.As(err, &col) {
		t.Fatalf("want SeqCollisionError, got %v", err)
	}
	// The rejected event must not have been written.
	events, _ := st.LoadEvents("r")
	if len(events) != 1 || events[0].EventID != "e1" {
		t.Fatalf("collision event got persisted: %+v", events)
	}
}

func TestCreateRunErrors(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir)
	if err := st.CreateRun("r"); err != nil {
		t.Fatal(err)
	}
	if err := st.CreateRun("r"); !errors.Is(err, ErrExists) {
		t.Fatalf("want ErrExists, got %v", err)
	}
	for _, bad := range []string{"../escape", "a/b", "a b", "x\x00y"} {
		if err := st.CreateRun(bad); err == nil {
			t.Fatalf("run id %q should be rejected", bad)
		}
	}
	// Ensure path traversal never created anything outside the cache root.
	if _, err := os.Stat(filepath.Join(dir, "escape")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("traversal created a path: %v", err)
	}
}

func TestAppendToMissingRun(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir)
	err := st.AppendEvent(domain.Event{EventID: "e", SeqNo: 1, Type: domain.EventShardStarted, RunID: "nope", Shard: "a"})
	if !errors.Is(err, ErrNotFound) {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
}

func TestListRunsSorted(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir)
	for _, id := range []string{"run-c", "run-a", "run-b"} {
		if err := st.CreateRun(id); err != nil {
			t.Fatal(err)
		}
	}
	ids, err := st.ListRuns()
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"run-a", "run-b", "run-c"}
	if len(ids) != 3 {
		t.Fatalf("ids=%v", ids)
	}
	for i := range want {
		if ids[i] != want[i] {
			t.Fatalf("ids=%v, want %v", ids, want)
		}
	}
}

func TestAppendRunFinishedSeqMono(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir)
	_ = st.CreateRun("r")
	_ = st.AppendEvent(domain.Event{EventID: "e1", SeqNo: 41, Type: domain.EventShardStarted, RunID: "r", Shard: "a"})
	ev, err := st.AppendRunFinished("r", "fin", false, domain.RunPassed, "done")
	if err != nil {
		t.Fatal(err)
	}
	if ev.SeqNo != 42 {
		t.Fatalf("seq=%d, want 42", ev.SeqNo)
	}
	// Second call with same id is a duplicate no-op.
	_, err = st.AppendRunFinished("r", "fin", false, domain.RunPassed, "done")
	var dup *DuplicateEventError
	if !errors.As(err, &dup) {
		t.Fatalf("want duplicate, got %v", err)
	}
	events, _ := st.LoadEvents("r")
	if len(events) != 2 {
		t.Fatalf("events=%d, want 2 (duplicate not appended)", len(events))
	}
}

func TestMaxSeq(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir)
	_ = st.CreateRun("r")
	_ = st.AppendEvent(domain.Event{EventID: "a", SeqNo: 3, Type: domain.EventShardStarted, RunID: "r", Shard: "a"})
	_ = st.AppendEvent(domain.Event{EventID: "b", SeqNo: 17, Type: domain.EventShardFinished, RunID: "r", Shard: "a"})
	max, err := st.MaxSeq("r")
	if err != nil || max != 17 {
		t.Fatalf("max=%d err=%v", max, err)
	}
}

func TestExecutionsAuditLog(t *testing.T) {
	dir := t.TempDir()
	st, _ := Open(dir)
	_ = st.CreateRun("r")
	if err := st.AppendExecution("r", map[string]any{"execution_id": "x1", "exit_code": 0}); err != nil {
		t.Fatal(err)
	}
	recs, err := st.LoadExecutions("r")
	if err != nil || len(recs) != 1 {
		t.Fatalf("recs=%d err=%v", len(recs), err)
	}
}
