package store

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"logpipe/internal/merger"
)

func appendRaw(path string, data []byte) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = f.Write(data)
	return err
}

func sample(i int64) merger.Entry {
	return merger.Entry{
		Source:    "s",
		StartTime: time.Date(2026, 9, 24, 10, 0, int(i%60), 0, time.UTC),
		EndTime:   time.Date(2026, 9, 24, 10, 0, int(i%60), 0, time.UTC),
		Text:      "entry-" + string(rune('a'+int(i))),
		LineCount: 1,
		Complete:  true,
		Reason:    merger.ReasonTimeout,
	}
}

func TestAppendQueryAndReplay(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "data")

	s, err := Open(dir)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	for i := int64(0); i < 3; i++ {
		e, err := s.Append(sample(i))
		if err != nil {
			t.Fatalf("append: %v", err)
		}
		if e.ID != i+1 {
			t.Fatalf("id = %d, want %d", e.ID, i+1)
		}
	}
	if s.Count() != 3 {
		t.Fatalf("count = %d, want 3", s.Count())
	}
	other, err := s.Append(func() merger.Entry {
		e := sample(9)
		e.Source = "other"
		return e
	}())
	if err != nil {
		t.Fatalf("append other: %v", err)
	}
	if got := s.Query("s", 0); len(got) != 3 {
		t.Fatalf("query source s = %d rows, want 3", len(got))
	}
	if got := s.Query("other", 0); len(got) != 1 || got[0].ID != other.ID {
		t.Fatalf("query other = %+v", got)
	}
	if got := s.Query("", 2); len(got) != 2 || got[0].ID != 3 || got[1].ID != 4 {
		t.Fatalf("limit query = %+v, want last 2 entries", got)
	}
	if err := s.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	// Reopen: entries and id sequence must survive.
	s2, err := Open(dir)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	defer s2.Close()
	if s2.Count() != 4 {
		t.Fatalf("after replay count = %d, want 4", s2.Count())
	}
	e, err := s2.Append(sample(20))
	if err != nil {
		t.Fatalf("append after replay: %v", err)
	}
	if e.ID != 5 {
		t.Fatalf("id after replay = %d, want 5", e.ID)
	}
}

func TestTornLineIsSkipped(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "data")
	s, err := Open(dir)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	if _, err := s.Append(sample(0)); err != nil {
		t.Fatalf("append: %v", err)
	}
	if err := s.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	// Simulate a torn append at the end of the log.
	path := filepath.Join(dir, "entries.jsonl")
	if err := appendRaw(path, []byte("{\"source\":\"broken\",\"text\":\"torn\n")); err != nil {
		t.Fatalf("append torn line: %v", err)
	}
	s2, err := Open(dir)
	if err != nil {
		t.Fatalf("reopen with torn line: %v", err)
	}
	defer s2.Close()
	if s2.Count() != 1 {
		t.Fatalf("count = %d, want 1 (torn line skipped)", s2.Count())
	}
}
