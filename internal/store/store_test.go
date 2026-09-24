package store

import (
	"path/filepath"
	"testing"
	"time"

	"dagexec/internal/dag"
)

func newState(id string) *dag.DAGState {
	now := time.Now().UTC()
	return &dag.DAGState{
		ID:     id,
		Status: dag.DAGPending,
		Spec: dag.Spec{Nodes: []dag.NodeSpec{
			{ID: "a", Task: "noop", MaxAttempts: 1},
		}, MaxAttempts: 1, MaxParallel: 1},
		Nodes:     map[string]*dag.NodeState{"a": {Status: dag.StatusPending}},
		CreatedAt: now,
		UpdatedAt: now,
	}
}

func TestRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	s1, err := NewFileStore(path)
	if err != nil {
		t.Fatal(err)
	}
	st := newState("dag1")
	if err := s1.Create(st); err != nil {
		t.Fatal(err)
	}
	if err := s1.Update("dag1", func(st *dag.DAGState) error {
		st.Status = dag.DAGSucceeded
		st.Nodes["a"].Status = dag.StatusSuccess
		st.Nodes["a"].Result = int64(42)
		return nil
	}); err != nil {
		t.Fatal(err)
	}

	// Simulate process restart: a fresh store reads the same file.
	s2, err := NewFileStore(path)
	if err != nil {
		t.Fatal(err)
	}
	got, err := s2.Get("dag1")
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != dag.DAGSucceeded || got.Nodes["a"].Result != float64(42) {
		t.Fatalf("state not restored: %#v", got)
	}
}

func TestMissingFileIsEmpty(t *testing.T) {
	s, err := NewFileStore(filepath.Join(t.TempDir(), "nested", "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	list, err := s.List()
	if err != nil || len(list) != 0 {
		t.Fatalf("want empty store, got %v %v", list, err)
	}
}

func TestUnknownID(t *testing.T) {
	s, _ := NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if _, err := s.Get("nope"); err != ErrNotFound {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
}

func TestGetReturnsCopy(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	s, _ := NewFileStore(path)
	if err := s.Create(newState("d")); err != nil {
		t.Fatal(err)
	}
	cp, _ := s.Get("d")
	cp.Nodes["a"].Status = dag.StatusSuccess
	again, _ := s.Get("d")
	if again.Nodes["a"].Status != dag.StatusPending {
		t.Fatal("Get must return a deep copy")
	}
}
