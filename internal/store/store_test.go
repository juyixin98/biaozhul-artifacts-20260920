package store

import (
	"path/filepath"
	"testing"
	"time"

	"logmerge/internal/merger"
)

func entry(src, msg string) merger.Entry {
	return merger.Entry{
		Source:       src,
		Message:      msg,
		StartTime:    time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC),
		EndTime:      time.Date(2026, 9, 24, 10, 0, 1, 0, time.UTC),
		LineCount:    1,
		Complete:     true,
		Reason:       merger.ReasonNewStart,
		HasStartLine: true,
	}
}

// TestRestartRecovery simulates a process restart: entries appended by
// one Store instance must be visible to a new Store opened on the same
// file.
func TestRestartRecovery(t *testing.T) {
	path := filepath.Join(t.TempDir(), "entries.jsonl")

	s1, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := s1.Append(entry("a", "before restart")); err != nil {
		t.Fatal(err)
	}
	if err := s1.Append(entry("b", "also before restart")); err != nil {
		t.Fatal(err)
	}

	// "Restart": open a brand-new store on the same file.
	s2, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	got := s2.List("", 0)
	if len(got) != 2 {
		t.Fatalf("expected 2 entries after restart, got %d", len(got))
	}
	if got[0].Message != "before restart" || got[1].Source != "b" {
		t.Fatalf("unexpected entries after restart: %+v", got)
	}

	// Appends after restart keep working.
	if err := s2.Append(entry("a", "after restart")); err != nil {
		t.Fatal(err)
	}
	if n := len(s2.List("a", 0)); n != 2 {
		t.Fatalf("expected 2 entries for source a, got %d", n)
	}
}

func TestListFilterAndLimit(t *testing.T) {
	s, err := Open(filepath.Join(t.TempDir(), "entries.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range []merger.Entry{
		entry("a", "1"), entry("b", "2"), entry("a", "3"),
	} {
		if err := s.Append(e); err != nil {
			t.Fatal(err)
		}
	}
	if got := s.List("a", 0); len(got) != 2 || got[0].Message != "1" || got[1].Message != "3" {
		t.Fatalf("filter wrong: %+v", got)
	}
	if got := s.List("", 2); len(got) != 2 || got[0].Message != "2" {
		t.Fatalf("limit should return newest 2: %+v", got)
	}
}
