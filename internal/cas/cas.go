// Package cas is a content-addressed store for build artifacts.
// Artifacts are written to a temp file, fsynced, then atomically renamed
// to cas/<sha256>. Entries whose stored bytes no longer match their
// recorded digest are moved to a quarantine directory.
package cas

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"
)

type Store struct {
	root string // data dir; holds cas/, quarantine/, tmp/
}

func Open(root string) (*Store, error) {
	for _, d := range []string{"cas", "quarantine", "tmp"} {
		if err := os.MkdirAll(filepath.Join(root, d), 0o755); err != nil {
			return nil, err
		}
	}
	return &Store{root: root}, nil
}

func (s *Store) Path(digest string) string {
	return filepath.Join(s.root, "cas", digest)
}

// Put streams r into the store, returning the digest and size of the
// stored object. If the object already exists it is left untouched.
func (s *Store) Put(r io.Reader) (string, int64, error) {
	tmp, err := os.CreateTemp(filepath.Join(s.root, "tmp"), "put-*")
	if err != nil {
		return "", 0, err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)

	h := sha256.New()
	n, err := io.Copy(io.MultiWriter(tmp, h), r)
	if err != nil {
		tmp.Close()
		return "", 0, err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return "", 0, err
	}
	if err := tmp.Close(); err != nil {
		return "", 0, err
	}
	digest := hex.EncodeToString(h.Sum(nil))
	dst := s.Path(digest)
	if _, err := os.Stat(dst); err == nil {
		return digest, n, nil // already stored
	}
	if err := os.Rename(tmpName, dst); err != nil {
		return "", 0, err
	}
	return digest, n, nil
}

// Verify re-hashes the stored object and compares it to the expected
// digest and size. A missing or corrupt object yields an error.
func (s *Store) Verify(digest string, wantSize int64) error {
	f, err := os.Open(s.Path(digest))
	if err != nil {
		return fmt.Errorf("artifact %s unreadable: %w", digest[:12], err)
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return fmt.Errorf("artifact %s read error: %w", digest[:12], err)
	}
	if n != wantSize {
		return fmt.Errorf("artifact %s size mismatch: got %d, want %d", digest[:12], n, wantSize)
	}
	if got := hex.EncodeToString(h.Sum(nil)); got != digest {
		return fmt.Errorf("artifact %s digest mismatch: stored bytes hash to %s", digest[:12], got[:12])
	}
	return nil
}

// Quarantine moves a corrupt object out of the cas/ directory so it can
// never be served again. Returns the quarantine path, or "" if the object
// was already gone.
func (s *Store) Quarantine(digest string) (string, error) {
	src := s.Path(digest)
	if _, err := os.Stat(src); os.IsNotExist(err) {
		return "", nil
	}
	dst := filepath.Join(s.root, "quarantine",
		fmt.Sprintf("%s.%d", digest, time.Now().UnixNano()))
	if err := os.Rename(src, dst); err != nil {
		return "", err
	}
	return dst, nil
}
