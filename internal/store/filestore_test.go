package store_test

import (
	"errors"
	"os"
	"testing"

	"dagexec/internal/model"
	"dagexec/internal/store"
)

func TestSaveLoadListRoundTrip(t *testing.T) {
	dir := t.TempDir()
	st, err := store.NewFileStore(dir)
	if err != nil {
		t.Fatal(err)
	}

	ids, err := st.List()
	if err != nil {
		t.Fatal(err)
	}
	if len(ids) != 0 {
		t.Fatalf("empty dir listed %v", ids)
	}

	snap := &model.Snapshot{ID: "abc", Status: model.StatusRunning}
	if err := st.Save("abc", snap); err != nil {
		t.Fatal(err)
	}

	var got model.Snapshot
	if err := st.Load("abc", &got); err != nil {
		t.Fatal(err)
	}
	if got.ID != "abc" || got.Status != model.StatusRunning {
		t.Fatalf("round trip mismatch: %+v", got)
	}

	// Atomic overwrite leaves no .tmp files.
	snap.Status = model.StatusSucceeded
	if err := st.Save("abc", snap); err != nil {
		t.Fatal(err)
	}
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if e.Name() != "abc.json" {
			t.Errorf("unexpected file after overwrite: %s", e.Name())
		}
	}

	ids, _ = st.List()
	if len(ids) != 1 || ids[0] != "abc" {
		t.Fatalf("list=%v", ids)
	}
}

func TestLoadNotFound(t *testing.T) {
	st, _ := store.NewFileStore(t.TempDir())
	var snap model.Snapshot
	err := st.Load("missing", &snap)
	if !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("err=%v want ErrNotFound", err)
	}
}

func TestSaveRejectsBadID(t *testing.T) {
	st, _ := store.NewFileStore(t.TempDir())
	if err := st.Save("../escape", struct{}{}); err == nil {
		t.Fatal("path-traversal id accepted")
	}
}
