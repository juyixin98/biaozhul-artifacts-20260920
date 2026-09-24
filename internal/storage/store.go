// Package storage implements the on-disk content-addressable blob store.
//
// Layout under Root:
//
//	blobs/sha256/<ab>/<full-digest>   published, immutable blobs
//	tmp/uploads/<upload-id>            staged, not yet verified/published
//	tmp/trash/<name>                   files unlinked from the CAS (best effort)
//
// Uploads are always staged under tmp first; Publish verifies the SHA-256
// digest *before* a byte is placed into blobs/, and the final move is a
// same-filesystem rename (atomic on POSIX).
package storage

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"layer-gc/internal/digest"
)

type Store struct {
	root  string
	blobs string
	tmp   string
	trash string
}

// New creates the directory skeleton and returns a store rooted at root.
func New(root string) (*Store, error) {
	s := &Store{
		root:  root,
		blobs: filepath.Join(root, "blobs", "sha256"),
		tmp:   filepath.Join(root, "tmp", "uploads"),
		trash: filepath.Join(root, "tmp", "trash"),
	}
	for _, d := range []string{s.blobs, s.tmp, s.trash} {
		if err := os.MkdirAll(d, 0o750); err != nil {
			return nil, fmt.Errorf("create storage dir %s: %w", d, err)
		}
	}
	return s, nil
}

// ErrNotFound means a published blob is absent on disk.
var ErrNotFound = errors.New("blob not found in content store")

// Upload is a staged, not-yet-published stream.
type Upload struct {
	ID   string
	path string
	f    *os.File
}

// BeginUpload creates a fresh temp file and returns an open handle.
func (s *Store) BeginUpload() (*Upload, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return nil, fmt.Errorf("generate upload id: %w", err)
	}
	id := hex.EncodeToString(b[:])
	path := filepath.Join(s.tmp, id)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_EXCL, 0o640)
	if err != nil {
		return nil, fmt.Errorf("create temp file: %w", err)
	}
	return &Upload{ID: id, path: path, f: f}, nil
}

// NewUpload wraps an already existing staged file path (DB-driven reopen
// returns a read-only handle via OpenUpload; this wraps a known writable one).
func NewUpload(id, path string) *Upload { return &Upload{ID: id, path: path} }

// OpenUpload reopens an existing staged upload by id (used with the DB row
// that records temp_path; here paths are deterministic from the id).
func (s *Store) OpenUpload(id string) (*Upload, error) {
	path := filepath.Join(s.tmp, id)
	f, err := os.Open(path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &Upload{ID: id, path: path, f: f}, nil
}

// Write appends to the staged file.
func (u *Upload) Write(p []byte) (int, error) { return u.f.Write(p) }

// ReadFrom streams r into the staged file, then fsyncs it so the bytes are
// durable before digest verification and publication.
func (u *Upload) ReadFrom(r io.Reader) (int64, error) {
	n, err := u.f.ReadFrom(r)
	if err != nil {
		return n, err
	}
	if ferr := u.f.Sync(); ferr != nil {
		return n, fmt.Errorf("fsync staged upload: %w", ferr)
	}
	return n, nil
}

// Close releases the handle (does not delete the temp file).
func (u *Upload) Close() error {
	if u.f == nil {
		return nil
	}
	err := u.f.Close()
	u.f = nil
	return err
}

// Path exposes the temp path for DB bookkeeping.
func (u *Upload) Path() string { return u.path }

// Verify re-reads the staged file and checks its digest.  Mismatched or
// malformed content is rejected and the temp file is removed.
func (s *Store) Verify(u *Upload, wantDigest string) (int64, error) {
	if _, err := digest.Check(wantDigest); err != nil {
		return 0, err
	}
	if err := u.Close(); err != nil {
		return 0, err
	}
	f, err := os.Open(u.path)
	if err != nil {
		return 0, fmt.Errorf("reopen staged upload: %w", err)
	}
	defer f.Close()
	got, n, err := digest.FromReader(f)
	if err != nil {
		return 0, err
	}
	if got != wantDigest {
		_ = os.Remove(u.path)
		return n, fmt.Errorf("digest mismatch: content hashes to %s, client claimed %s", got, wantDigest)
	}
	return n, nil
}

// Publish moves a verified staged file into the content-addressable store.
// It is idempotent: if the destination already exists (identical content,
// since the address is the digest), the staged file is discarded.
func (s *Store) Publish(u *Upload, d string) error {
	if _, err := digest.Check(d); err != nil {
		return err
	}
	dst := s.blobPath(d)
	if err := os.MkdirAll(filepath.Dir(dst), 0o750); err != nil {
		return fmt.Errorf("create blob dir: %w", err)
	}
	// Same-directory rename across the store could cross dirs; ensure the
	// source and target live on one filesystem (they share Root).
	if err := os.Link(u.path, dst); err == nil {
		// Hard-linked: content is now at its CAS address; drop the temp name.
		if err := os.Remove(u.path); err != nil && !errors.Is(err, fs.ErrNotExist) {
			return fmt.Errorf("unlink staged upload after publish: %w", err)
		}
		return s.fsyncDir(s.blobs)
	} else if !errors.Is(err, fs.ErrExist) {
		return fmt.Errorf("publish blob (link): %w", err)
	}
	// Destination already exists: identical digest means identical content.
	if err := os.Remove(u.path); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("unlink duplicate staged upload: %w", err)
	}
	return nil
}

// DiscardUpload removes a staged temp file (cancel/abort).
func (s *Store) DiscardUpload(id string) error {
	err := os.Remove(filepath.Join(s.tmp, id))
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	return err
}

func (s *Store) fsyncDir(dir string) error {
	f, err := os.Open(dir)
	if err != nil {
		return nil // best effort
	}
	defer f.Close()
	return f.Sync()
}

// blobPath maps a digest to its sharded CAS path.
func (s *Store) blobPath(d string) string {
	return filepath.Join(s.blobs, d[len(digest.Prefix):len(digest.Prefix)+2], d)
}

// Open opens a published blob for reading.
func (s *Store) Open(d string) (*os.File, error) {
	f, err := os.Open(s.blobPath(d))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, ErrNotFound
	}
	return f, err
}

// Stat returns metadata for a published blob.
func (s *Store) Stat(d string) (os.FileInfo, error) {
	fi, err := os.Stat(s.blobPath(d))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, ErrNotFound
	}
	return fi, err
}

// Exists reports whether the blob is present in the CAS.
func (s *Store) Exists(d string) bool {
	_, err := os.Stat(s.blobPath(d))
	return err == nil
}

// Delete removes a published blob file from the CAS.
func (s *Store) Delete(d string) error {
	err := os.Remove(s.blobPath(d))
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	return err
}

// MoveToTrash moves a blob out of the CAS into the trash dir instead of
// unlinking it outright; returns the trash path. Used when the caller wants
// a recovery window. Callers that want instant free space use Delete.
func (s *Store) MoveToTrash(d string) (string, error) {
	src := s.blobPath(d)
	name := strings.ReplaceAll(d, ":", "_")
	dst := filepath.Join(s.trash, name)
	if err := os.Rename(src, dst); err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return "", nil
		}
		return "", err
	}
	return dst, nil
}

// StagedFiles lists files currently in the temp upload directory.
func (s *Store) StagedFiles() ([]string, error) {
	entries, err := os.ReadDir(s.tmp)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, e := range entries {
		if !e.IsDir() {
			out = append(out, filepath.Join(s.tmp, e.Name()))
		}
	}
	return out, nil
}

// TmpDir returns the upload temp directory.
func (s *Store) TmpDir() string { return s.tmp }

// PublishedBlobs returns the digests of every blob file present in the CAS,
// by walking the sharded layout.  Used by the orphan reconciler to compare
// the filesystem against the database.
func (s *Store) PublishedBlobs() ([]string, error) {
	var out []string
	err := filepath.WalkDir(s.blobs, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		name := d.Name()
		if digest.Valid(name) {
			out = append(out, name)
		}
		return nil
	})
	return out, err
}
