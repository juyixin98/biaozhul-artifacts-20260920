package wal_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"tpc/internal/wal"
)

type rec struct {
	Kind string `json:"kind"`
	V    int    `json:"v"`
}

func TestAppendAndReplay(t *testing.T) {
	dir := t.TempDir()
	w, err := wal.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	want := []rec{{Kind: "a", V: 1}, {Kind: "b", V: 2}, {Kind: "c", V: 3}}
	for _, r := range want {
		if err := w.Append(r); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}

	w, err = wal.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	var got []rec
	var cur rec
	if err := w.Replay(&cur, func() error {
		got = append(got, cur)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if len(got) != len(want) {
		t.Fatalf("got %d records, want %d", len(got), len(want))
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("record %d = %+v, want %+v", i, got[i], want[i])
		}
	}
}

// TestTornTailTruncated simulates a crash mid-write: a partial JSON line
// without a newline is appended directly to the file. Reopen must truncate
// it and the previously fsync'd records must survive intact.
func TestTornTailTruncated(t *testing.T) {
	dir := t.TempDir()
	w, err := wal.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := w.Append(rec{Kind: "durable", V: 9}); err != nil {
		t.Fatal(err)
	}
	w.Close()

	// Write garbage resembling a torn record (no trailing newline).
	path := filepath.Join(dir, "wal.log")
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.Write([]byte(`{"kind":"torn","v":`)); err != nil {
		t.Fatal(err)
	}
	f.Close()

	w, err = wal.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	var got []rec
	var cur rec
	if err := w.Replay(&cur, func() error { got = append(got, cur); return nil }); err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Kind != "durable" {
		t.Fatalf("replay after torn tail = %+v, want only the durable record", got)
	}

	// The log must remain appendable after repair.
	if err := w.Append(rec{Kind: "after", V: 10}); err != nil {
		t.Fatal(err)
	}
}

// TestLongTornTailTruncated covers a torn tail longer than the repair
// block size (8 KiB): the scan must reach the beginning and truncate the
// whole tail, rather than giving up mid-file.
func TestLongTornTailTruncated(t *testing.T) {
	dir := t.TempDir()
	w, err := wal.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	type big struct {
		Kind string `json:"kind"`
		Pad  string `json:"pad"`
	}
	if err := w.Append(big{Kind: "durable", Pad: strings.Repeat("z", 20000)}); err != nil {
		t.Fatal(err)
	}
	w.Close()

	path := filepath.Join(dir, "wal.log")
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.Write([]byte(`{"kind":"` + strings.Repeat("t", 20000))); err != nil {
		t.Fatal(err)
	}
	f.Close()

	w, err = wal.Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	var got []big
	var cur big
	if err := w.Replay(&cur, func() error { got = append(got, cur); return nil }); err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Kind != "durable" || len(got[0].Pad) != 20000 {
		t.Fatalf("replay after long torn tail got %d records", len(got))
	}
}
