package store

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestSaveListGetDelete(t *testing.T) {
	st, err := Open(t.TempDir())
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	id, err := st.Save("a@example", []string{"b@example", "c@example"},
		[]byte("Subject: hi\r\n\r\nbody\r\n"), time.Now())
	if err != nil {
		t.Fatalf("save: %v", err)
	}
	if id == "" {
		t.Fatal("empty id")
	}
	msgs := st.List()
	if len(msgs) != 1 || msgs[0].From != "a@example" || len(msgs[0].To) != 2 {
		t.Fatalf("unexpected list: %#v", msgs)
	}
	if msgs[0].Raw != nil {
		t.Fatal("List must not include raw payloads")
	}
	m, err := st.Get(id)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if string(m.Raw) != "Subject: hi\r\n\r\nbody\r\n" {
		t.Fatalf("raw mismatch: %q", m.Raw)
	}
	if err := st.Delete(id); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if _, err := st.Get(id); !errors.Is(err, ErrNotFound) {
		t.Fatalf("expected ErrNotFound after delete, got %v", err)
	}
	if err := st.Delete(id); !errors.Is(err, ErrNotFound) {
		t.Fatalf("delete missing should be ErrNotFound, got %v", err)
	}
}

func TestFileIsCompleteAndAtomic(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "spool")
	st, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	id, err := st.Save("a@example", []string{"b@example"}, []byte("payload"), time.Now())
	if err != nil {
		t.Fatal(err)
	}
	// Final file exists under msgs/, no leftovers in tmp/.
	if _, err := os.Stat(filepath.Join(dir, msgDir, id+msgExt)); err != nil {
		t.Fatalf("final file missing: %v", err)
	}
	tmps, _ := os.ReadDir(filepath.Join(dir, tmpDir))
	if len(tmps) != 0 {
		t.Fatalf("temp dir not clean: %v", tmps)
	}
}

func TestOrphanTempSweptOnOpen(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "spool")
	st, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.Save("a@example", []string{"b@example"}, []byte("one"), time.Now()); err != nil {
		t.Fatal(err)
	}
	// Simulate a crash mid-DATA: leave a stray temp file behind.
	stray := filepath.Join(dir, tmpDir, "20260101T000000-00000000000000000000000000000000"+tmpExt)
	if err := os.WriteFile(stray, []byte("partial"), 0o644); err != nil {
		t.Fatal(err)
	}
	st2, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(st2.List()) != 1 {
		t.Fatalf("stray temp must not become a message; list=%#v", st2.List())
	}
	if _, err := os.Stat(stray); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("stray temp not swept: %v", err)
	}
}

func TestIndexRebuiltOnRestart(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "spool")
	st, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	id1, _ := st.Save("a@example", []string{"b@example"}, []byte("first"), time.Now())
	id2, _ := st.Save("c@example", []string{"d@example"}, []byte("second"), time.Now())

	st2, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	list := st2.List()
	if len(list) != 2 {
		t.Fatalf("want 2 recovered, got %d", len(list))
	}
	// IDs are timestamp-prefixed so order is deterministic.
	if list[0].ID != id1 || list[1].ID != id2 {
		t.Fatalf("recovered order wrong: %s %s", list[0].ID, list[1].ID)
	}
	m, err := st2.Get(id2)
	if err != nil || string(m.Raw) != "second" {
		t.Fatalf("content not recovered: %v %q", err, m)
	}
}

func TestConcurrentSaves(t *testing.T) {
	st, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	const n = 20
	done := make(chan string, n)
	for i := 0; i < n; i++ {
		go func(i int) {
			id, err := st.Save("a@example", []string{"b@example"}, []byte("x"), time.Now())
			if err != nil {
				t.Errorf("save %d: %v", i, err)
			}
			done <- id
		}(i)
	}
	ids := map[string]bool{}
	for i := 0; i < n; i++ {
		id := <-done
		if ids[id] {
			t.Fatalf("duplicate id %q", id)
		}
		ids[id] = true
	}
	if len(st.List()) != n {
		t.Fatalf("want %d messages, got %d", n, len(st.List()))
	}
}
