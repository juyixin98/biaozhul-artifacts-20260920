package cache

import (
	"os"
	"path/filepath"
	"testing"

	"cdag/internal/fingerprint"
)

func TestWriteAndRestore(t *testing.T) {
	root := t.TempDir()
	c, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	work := filepath.Join(t.TempDir(), "ws")
	if err := os.MkdirAll(filepath.Join(work, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(work, "sub", "o.txt"), []byte("abc"), 0o644); err != nil {
		t.Fatal(err)
	}
	refs, err := fingerprint.HashFiles(work, []string{"sub/o.txt"})
	if err != nil {
		t.Fatal(err)
	}
	fp := fingerprint.Fingerprint{NodeID: "n"}
	key, _ := fp.Key()

	if c.Has(key) {
		t.Fatal("entry must not exist yet")
	}
	if err := c.WriteEntry(key, "n", work, refs, fp); err != nil {
		t.Fatal(err)
	}
	if !c.Has(key) {
		t.Fatal("entry must exist after write")
	}

	// Corrupt the workspace copy, then restore.
	if err := os.WriteFile(filepath.Join(work, "sub", "o.txt"), []byte("CORRUPT"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Restore(key, work); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(filepath.Join(work, "sub", "o.txt"))
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "abc" {
		t.Fatalf("restore = %q, want abc", got)
	}
}

func TestIdempotentWrite(t *testing.T) {
	root := t.TempDir()
	c, _ := Open(root)
	work := t.TempDir()
	if err := os.WriteFile(filepath.Join(work, "o"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	refs, _ := fingerprint.HashFiles(work, []string{"o"})
	key, _ := fingerprint.Fingerprint{NodeID: "n"}.Key()
	if err := c.WriteEntry(key, "n", work, refs, fingerprint.Fingerprint{NodeID: "n"}); err != nil {
		t.Fatal(err)
	}
	if err := c.WriteEntry(key, "n", work, refs, fingerprint.Fingerprint{NodeID: "n"}); err != nil {
		t.Fatalf("second write should be a no-op: %v", err)
	}
}

func TestHistoryRoundTrip(t *testing.T) {
	root := t.TempDir()
	c, _ := Open(root)
	if _, ok, err := c.HistoryRecord("p", "n"); err != nil || ok {
		t.Fatalf("fresh history: ok=%v err=%v", ok, err)
	}
	rec := HistoryRecord{NodeID: "n", LastKey: "k1", LastStatus: "success"}
	if err := c.SetHistoryRecord("p", rec); err != nil {
		t.Fatal(err)
	}
	got, ok, err := c.HistoryRecord("p", "n")
	if err != nil || !ok || got.LastKey != "k1" {
		t.Fatalf("history round trip: %+v ok=%v err=%v", got, ok, err)
	}
	// Project isolation.
	if _, ok, _ := c.HistoryRecord("other", "n"); ok {
		t.Fatal("history must be per-project")
	}
}

func TestContentAddressingSharedAcrossKeys(t *testing.T) {
	// Different fingerprints => different keys, same content stored twice
	// independently; identical fingerprints share an entry.
	root := t.TempDir()
	c, _ := Open(root)
	work := t.TempDir()
	os.WriteFile(filepath.Join(work, "o"), []byte("data"), 0o644)
	refs, _ := fingerprint.HashFiles(work, []string{"o"})

	k1, _ := fingerprint.Fingerprint{NodeID: "a"}.Key()
	k2, _ := fingerprint.Fingerprint{NodeID: "b"}.Key()
	if k1 == k2 {
		t.Fatal("different fingerprints must produce different keys")
	}
	if err := c.WriteEntry(k1, "a", work, refs, fingerprint.Fingerprint{NodeID: "a"}); err != nil {
		t.Fatal(err)
	}
	if err := c.WriteEntry(k2, "b", work, refs, fingerprint.Fingerprint{NodeID: "b"}); err != nil {
		t.Fatal(err)
	}
	if !c.Has(k1) || !c.Has(k2) {
		t.Fatal("both entries should exist")
	}
}
