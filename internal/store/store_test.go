package store

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPutGetRoundTrip(t *testing.T) {
	st, err := Open(filepath.Join(t.TempDir(), "cache"))
	if err != nil {
		t.Fatal(err)
	}
	content := bytes.Repeat([]byte("artifact-content-"), 500)
	want := sha256.Sum256(content)
	wantHex := hex.EncodeToString(want[:])

	h, err := st.Put(bytes.NewReader(content), "")
	if err != nil {
		t.Fatal(err)
	}
	if h != wantHex {
		t.Fatalf("Put returned %s want %s", h, wantHex)
	}
	rc, err := st.OpenForRead(h)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := io.ReadAll(rc)
	rc.Close()
	if !bytes.Equal(got, content) {
		t.Fatal("content mismatch")
	}

	// Put with expected hash mismatch must error and not store.
	if _, err := st.Put(strings.NewReader("other"), wantHex); err == nil {
		t.Fatal("expected hash mismatch error")
	}

	// Put same content twice is idempotent and atomic.
	h2, err := st.Put(bytes.NewReader(content), wantHex)
	if err != nil || h2 != h {
		t.Fatalf("idempotent put failed: %v %s", err, h2)
	}
}

func TestPathTraversalRejected(t *testing.T) {
	st, _ := Open(filepath.Join(t.TempDir(), "cache"))
	if _, err := st.Path("../etc/passwd"); err == nil {
		t.Fatal("expected invalid hash error")
	}
	if _, err := st.Path(strings.Repeat("a", 63)); err == nil {
		t.Fatal("expected invalid hash length error")
	}
}

func TestResolve(t *testing.T) {
	st, _ := Open(filepath.Join(t.TempDir(), "cache"))
	// Relative path is rejected (must be hash or absolute).
	if _, _, err := st.Resolve("relative/path.bin"); err == nil {
		t.Fatal("expected error for relative ref")
	}
	// Unknown hash.
	if _, _, err := st.Resolve(strings.Repeat("f", 64)); err == nil {
		t.Fatal("expected not-found for unknown hash")
	}
	// Absolute file path works.
	dir := t.TempDir()
	p := filepath.Join(dir, "a.bin")
	os.WriteFile(p, []byte("absolute"), 0o644)
	r, n, err := st.Resolve(p)
	if err != nil {
		t.Fatal(err)
	}
	if n != 8 {
		t.Fatalf("size %d", n)
	}
	r.Close()
}
