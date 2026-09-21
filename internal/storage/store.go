// Package storage implements the on-disk, content-addressed blob store.
//
// Files live under <data>/blobs/<ab>/<64-hex-sha256> and are immutable: the
// hash is the only name, so identical chunks or datasets from different users
// share one inode. Incoming bytes always land in <data>/tmp first; they only
// become a blob via a hard link, which is atomic on the same filesystem.
// Blob rows in PostgreSQL are the authority for liveness (reference counted),
// and every link is created before its row is inserted inside the same
// transaction. A crash can therefore leave orphan files (cleaned at startup)
// but never a row without its file in normal operation.
package storage

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// Store manages blob and temp directories under Root.
type Store struct {
	Root string
}

// New creates the directory layout and returns a Store.
func New(root string) (*Store, error) {
	s := &Store{Root: root}
	for _, sub := range []string{"blobs", "tmp"} {
		if err := os.MkdirAll(filepath.Join(root, sub), 0o750); err != nil {
			return nil, fmt.Errorf("create %s dir: %w", sub, err)
		}
	}
	return s, nil
}

// BlobPath returns the canonical path of a blob (it may not exist yet).
func (s *Store) BlobPath(sha string) string {
	return filepath.Join(s.Root, "blobs", sha[:2], sha)
}

// BlobRootForTest returns the blobs directory (used by tests/inspection).
func (s *Store) BlobRootForTest() string { return filepath.Join(s.Root, "blobs") }

// TempPath returns the path of a named temp file inside the temp directory.
func (s *Store) TempPath(name string) string {
	return filepath.Join(s.Root, "tmp", name)
}

// CreateTemp creates a new temp file with the given name. Callers are
// responsible for closing and removing it.
func (s *Store) CreateTemp(name string) (*os.File, error) {
	return os.OpenFile(s.TempPath(name), os.O_RDWR|os.O_CREATE|os.O_EXCL, 0o640)
}

// RemoveTemp removes a temp file; a missing file is not an error.
func (s *Store) RemoveTemp(name string) error {
	if err := os.Remove(s.TempPath(name)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// LinkIntoBlob hard-links src into the blob namespace. created is false when
// the blob path already existed (content equality is guaranteed by the
// content-addressed name). No data is copied.
func (s *Store) LinkIntoBlob(src, sha string) (created bool, err error) {
	dst := s.BlobPath(sha)
	if err := os.MkdirAll(filepath.Dir(dst), 0o750); err != nil {
		return false, fmt.Errorf("create blob shard dir: %w", err)
	}
	if err := os.Link(src, dst); err != nil {
		if errors.Is(err, os.ErrExist) {
			return false, nil
		}
		return false, fmt.Errorf("link blob: %w", err)
	}
	return true, nil
}

// UnlinkBlob removes the physical blob file. Missing files are tolerated so
// garbage collection stays idempotent.
func (s *Store) UnlinkBlob(sha string) error {
	if err := os.Remove(s.BlobPath(sha)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// CleanupTemp removes every file from the temp directory. It runs at startup
// to discard writes left behind by a crashed process; temp files never hold
// the only copy of committed data.
func (s *Store) CleanupTemp() error {
	entries, err := os.ReadDir(filepath.Join(s.Root, "tmp"))
	if err != nil {
		return err
	}
	for _, e := range entries {
		if err := os.RemoveAll(filepath.Join(s.Root, "tmp", e.Name())); err != nil {
			return err
		}
	}
	return nil
}

// OrphanBlobs returns the hex hashes of files present on disk but absent from
// the given set of live blob hashes. Each shard directory is read in full.
func (s *Store) OrphanBlobs(live map[string]struct{}) ([]string, error) {
	var orphans []string
	blobsDir := filepath.Join(s.Root, "blobs")
	shards, err := os.ReadDir(blobsDir)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	for _, shard := range shards {
		if !shard.IsDir() || len(shard.Name()) != 2 {
			continue
		}
		files, err := os.ReadDir(filepath.Join(blobsDir, shard.Name()))
		if err != nil {
			return nil, err
		}
		for _, f := range files {
			name := f.Name()
			if f.IsDir() || len(name) != 64 {
				continue
			}
			if _, ok := live[name]; !ok {
				orphans = append(orphans, name)
			}
		}
	}
	sort.Strings(orphans)
	return orphans, nil
}

// SinkHash is the hex SHA-256 convention shared across the service.
func SuffixHex(sum [sha256.Size]byte) string { return hex.EncodeToString(sum[:]) }

// IsSHA256Hex reports whether s looks like a lowercase hex SHA-256 digest.
func IsSHA256Hex(s string) bool {
	if len(s) != 64 {
		return false
	}
	for _, r := range s {
		if !((r >= '0' && r <= '9') || (r >= 'a' && r <= 'f')) {
			return false
		}
	}
	return true
}

// CopyAndHash copies r into w while accumulating SHA-256 and bytes written.
func CopyAndHash(dst io.Writer, src io.Reader) (sha string, n int64, err error) {
	h := sha256.New()
	n, err = io.Copy(io.MultiWriter(dst, h), src)
	return hex.EncodeToString(h.Sum(nil)), n, err
}

// SafeName guards against path components sneaking into constructed names.
func SafeName(name string) bool {
	return name != "" && name != "." && name != ".." &&
		!strings.ContainsRune(name, os.PathSeparator) && filepath.Base(name) == name
}
