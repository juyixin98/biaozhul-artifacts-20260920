// Package store implements a content-addressed artifact cache.
//
// The cache lives entirely under its own root directory and is intentionally
// separate from the working directory: clients never receive a path inside the
// cache, only content hashes. Objects are written atomically (temp file +
// rename) and are immutable once stored.
package store

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// Store is a content-addressed object store.
type Store struct {
	root string
}

// Open (or creates) a store rooted at dir.
func Open(dir string) (*Store, error) {
	if dir == "" {
		return nil, errors.New("store: empty cache directory")
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("store: create cache dir: %w", err)
	}
	return &Store{root: dir}, nil
}

// Root returns the absolute cache root.
func (s *Store) Root() string { return s.root }

// Path returns the cache path for a hex SHA-256 hash. It rejects malformed
// hashes so a hash can never escape the cache root.
func (s *Store) Path(hexHash string) (string, error) {
	if !isValidHex(hexHash) {
		return "", fmt.Errorf("store: invalid hash %q", hexHash)
	}
	return filepath.Join(s.root, "sha256", hexHash[:2], hexHash), nil
}

// Has reports whether an object with the given hex hash exists.
func (s *Store) Has(hexHash string) (bool, error) {
	p, err := s.Path(hexHash)
	if err != nil {
		return false, err
	}
	if _, err := os.Stat(p); err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, err
	}
	return true, nil
}

// Stat returns the size of a stored object.
func (s *Store) Stat(hexHash string) (int64, error) {
	p, err := s.Path(hexHash)
	if err != nil {
		return 0, err
	}
	fi, err := os.Stat(p)
	if err != nil {
		return 0, err
	}
	return fi.Size(), nil
}

// Put streams r into the cache, verifying the content hashes to wantHex (when
// non-empty). It returns the hex SHA-256 of the stored content. The write is
// atomic: a concurrent/aborted write never leaves a partial object visible at
// its final path.
func (s *Store) Put(r io.Reader, wantHex string) (string, error) {
	dir := filepath.Join(s.root, "tmp")
	if err := os.MkdirAll(filepath.Join(s.root, "sha256"), 0o755); err != nil {
		return "", err
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}

	f, err := os.CreateTemp(dir, "put-*")
	if err != nil {
		return "", fmt.Errorf("store: create temp: %w", err)
	}
	tmpPath := f.Name()
	committed := false
	defer func() {
		if !committed {
			f.Close()
			os.Remove(tmpPath)
		}
	}()

	h := sha256.New()
	if _, err := io.Copy(io.MultiWriter(f, h), r); err != nil {
		return "", fmt.Errorf("store: write: %w", err)
	}
	if err := f.Sync(); err != nil {
		return "", fmt.Errorf("store: sync: %w", err)
	}
	if err := f.Close(); err != nil {
		return "", fmt.Errorf("store: close: %w", err)
	}

	got := hex.EncodeToString(h.Sum(nil))
	if wantHex != "" && got != wantHex {
		return "", fmt.Errorf("store: content hash mismatch: got %s want %s", got, wantHex)
	}

	final, err := s.Path(got)
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(filepath.Dir(final), 0o755); err != nil {
		return "", err
	}
	if err := os.Rename(tmpPath, final); err != nil {
		return "", fmt.Errorf("store: commit: %w", err)
	}
	committed = true
	return got, nil
}

// OpenForRead opens a stored object for reading. The returned reader must be
// closed by the caller.
func (s *Store) OpenForRead(hexHash string) (io.ReadCloser, error) {
	p, err := s.Path(hexHash)
	if err != nil {
		return nil, err
	}
	return os.Open(p)
}

// Resolve opens an artifact reference: either a 64-hex-char content hash in the
// cache or an absolute local file path. Relative paths are rejected — callers
// resolve user-relative paths against the working dir themselves.
func (s *Store) Resolve(ref string) (io.ReadSeekCloser, int64, error) {
	if isValidHex(ref) {
		p, err := s.Path(ref)
		if err != nil {
			return nil, 0, err
		}
		f, err := os.Open(p)
		if err != nil {
			return nil, 0, fmt.Errorf("artifact %s not in cache: %w", ref[:12], err)
		}
		fi, err := f.Stat()
		if err != nil {
			f.Close()
			return nil, 0, err
		}
		return f, fi.Size(), nil
	}
	if !filepath.IsAbs(ref) {
		return nil, 0, fmt.Errorf("artifact reference must be a hash or absolute path: %q", ref)
	}
	f, err := os.Open(ref)
	if err != nil {
		return nil, 0, err
	}
	fi, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, 0, err
	}
	if fi.IsDir() {
		f.Close()
		return nil, 0, fmt.Errorf("artifact path is a directory: %s", ref)
	}
	return f, fi.Size(), nil
}

func isValidHex(h string) bool {
	if len(h) != 64 {
		return false
	}
	for _, c := range h {
		if !strings.ContainsRune("0123456789abcdef", c) {
			return false
		}
	}
	return true
}
