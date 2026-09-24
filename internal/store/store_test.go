package store

import (
	"os"
	"path/filepath"
	"testing"
)

func TestAddListGetDelete(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "mail")
	st, err := Open(dir)
	if err != nil {
		t.Fatalf("open: %v", err)
	}

	m, err := st.Add("a@x", []string{"b@y", "c@y"}, "hello\r\nworld")
	if err != nil {
		t.Fatalf("add: %v", err)
	}
	if m.Bytes != len("hello\r\nworld") {
		t.Errorf("bytes = %d", m.Bytes)
	}
	if got := st.Len(); got != 1 {
		t.Fatalf("len = %d", got)
	}

	got, err := st.Get(m.ID)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if got.Data != "hello\r\nworld" || len(got.To) != 2 {
		t.Errorf("got = %+v", got)
	}

	removed, err := st.Delete(m.ID)
	if err != nil || !removed {
		t.Fatalf("delete = %v, %v", removed, err)
	}
	if _, err := st.Get(m.ID); err != ErrNotFound {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
}

func TestPersistenceAcrossReopen(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "mail")
	st, _ := Open(dir)
	id1, _ := st.Add("a@x", []string{"b@y"}, "one")
	id2, _ := st.Add("c@x", []string{"d@y"}, "two")

	st2, err := Open(dir)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	if st2.Len() != 2 {
		t.Fatalf("want 2 after reopen, got %d", st2.Len())
	}
	// Listings are newest-first: id2 before id1.
	list := st2.List(0)
	if list[0].ID != id2.ID || list[1].ID != id1.ID {
		t.Errorf("reload order wrong: %s, %s", list[0].ID, list[1].ID)
	}
	// Listings must not leak body data.
	if list[0].Data != "" {
		t.Errorf("listing included body: %q", list[0].Data)
	}
}

func TestNoTempFilesAndNoPartialRecords(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "mail")
	st, _ := Open(dir)
	st.Add("a@x", []string{"b@y"}, "payload")

	st2, _ := Open(dir)
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if filepath.Ext(e.Name()) == ".tmp" {
			t.Errorf("stale temp file survived: %s", e.Name())
		}
	}
	if st2.Len() != 1 {
		t.Fatalf("want 1, got %d", st2.Len())
	}
}
