package store

import (
	"os"
	"path/filepath"
	"testing"
)

func TestBlobContentAddressingAndDedup(t *testing.T) {
	st, err := Open(filepath.Join(t.TempDir(), "root"))
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	f1 := filepath.Join(dir, "f1")
	f2 := filepath.Join(dir, "f2")
	if err := os.WriteFile(f1, []byte("same bytes"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(f2, []byte("same bytes"), 0o644); err != nil {
		t.Fatal(err)
	}
	d1, err := st.PutBlob(f1)
	if err != nil {
		t.Fatal(err)
	}
	d2, err := st.PutBlob(f2)
	if err != nil {
		t.Fatal(err)
	}
	if !d1.Equal(d2) {
		t.Fatal("identical content produced different digests")
	}
	entries, _ := os.ReadDir(st.BlobsDir)
	if len(entries) != 1 {
		t.Fatalf("expected exactly one deduped blob, got %d", len(entries))
	}
	if !st.HasBlob(d1) {
		t.Fatal("HasBlob false for stored blob")
	}
}

func TestKeyStableAcrossReopen(t *testing.T) {
	root := filepath.Join(t.TempDir(), "root")
	st1, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	st2, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	if string(st1.Key()) != string(st2.Key()) {
		t.Fatal("HMAC key changed across reopen")
	}
}
