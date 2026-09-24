// Package storage manages the on-disk content-addressable layout:
//
//	root/blobs/sha256/<ab>/<full-digest>   published, immutable blobs
//	root/tmp/<upload-id>                   staged upload chunks (orphan candidates)
//	root/quarantine/<run-id>/<digest>      blobs detached from DB by a GC sweep,
//	                                       kept until the run commits cleanly
//
// Publishing is an atomic same-filesystem rename after real digest
// verification, so a crash at any point leaves either the temp file (orphan)
// or the published blob, never a corrupt published blob.
package storage

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"layerregistry/internal/digestx"
)

type Store struct {
	root       string
	blobsDir   string
	tmpDir     string
	quarantine string
}

func New(root string) (*Store, error) {
	s := &Store{
		root:       root,
		blobsDir:   filepath.Join(root, "blobs", "sha256"),
		tmpDir:     filepath.Join(root, "tmp"),
		quarantine: filepath.Join(root, "quarantine"),
	}
	for _, d := range []string{s.blobsDir, s.tmpDir, s.quarantine} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, err
		}
	}
	return s, nil
}

// blobPath must be called only after digest validation (path traversal safe).
func (s *Store) blobPath(digest string) string {
	_, hex, _ := digestx.Parse(digest)
	return filepath.Join(s.blobsDir, hex[:2], digest)
}

// TempPath resolves a server-generated session name inside the staging dir.
func (s *Store) TempPath(name string) string {
	return filepath.Join(s.tmpDir, filepath.Base(name))
}

func (s *Store) quarantinePath(runID, digest string) string {
	return filepath.Join(s.quarantine, filepath.Base(runID), filepath.Base(digest))
}

// fsyncDir flushes directory entry changes (rename/unlink) to disk.
func fsyncDir(dir string) error {
	f, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}

// CreateTemp creates a new staging file for an upload session.
func (s *Store) CreateTemp(name string) (*os.File, error) {
	return os.OpenFile(s.TempPath(name), os.O_CREATE|os.O_WRONLY|os.O_EXCL, 0o644)
}

// AppendChunk validates the append against the session offset, writes and
// fsyncs. Returns the new size of the staging file.
func (s *Store) AppendChunk(name string, expectOffset int64, r io.Reader) (int64, error) {
	p := s.TempPath(name)
	f, err := os.OpenFile(p, os.O_WRONLY, 0o644)
	if err != nil {
		return 0, err
	}
	off, err := f.Seek(0, io.SeekEnd)
	if err != nil {
		f.Close()
		return 0, err
	}
	if off != expectOffset {
		f.Close()
		return off, fmt.Errorf("offset mismatch: client expects %d, staged file is at %d", expectOffset, off)
	}
	n, err := io.Copy(f, r)
	if err != nil {
		f.Close()
		return off, err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return off, err
	}
	if err := f.Close(); err != nil {
		return off, err
	}
	return off + n, nil
}

// PublishTemp verifies the staged file against digest with a real SHA-256
// pass, then atomically renames it into the CAS tree. On digest mismatch the
// temp file is left in place and the caller abandons the upload.
func (s *Store) PublishTemp(name, digest string) (int64, error) {
	if !digestx.Valid(digest) {
		return 0, digestx.ErrDigestFormat
	}
	tmp := s.TempPath(name)
	f, err := os.Open(tmp)
	if err != nil {
		return 0, err
	}
	h := digestx.NewHasher(io.Discard)
	n, err := io.Copy(h, f)
	f.Close()
	if err != nil {
		return 0, err
	}
	if h.Digest() != digest {
		return 0, fmt.Errorf("%w: expected %s, computed %s", digestx.ErrDigestMismatch, digest, h.Digest())
	}

	dst := s.blobPath(digest)
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return 0, err
	}
	if s.BlobExists(digest) {
		// Idempotent concurrent publish: content is equal by definition.
		if rmErr := os.Remove(tmp); rmErr != nil && !errors.Is(rmErr, os.ErrNotExist) {
			return 0, rmErr
		}
		return n, nil
	}
	if err := os.Rename(tmp, dst); err != nil {
		return 0, err
	}
	if err := fsyncDir(filepath.Dir(dst)); err != nil {
		return 0, err
	}
	return n, nil
}

// StreamToTemp stages a monolithic upload into a fresh temp file while hashing
// it for real; returns the size and the computed digest. Verification against
// the client-claimed digest is the caller's job.
func (s *Store) StreamToTemp(name string, r io.Reader) (int64, string, error) {
	f, err := s.CreateTemp(name)
	if err != nil {
		return 0, "", err
	}
	h := digestx.NewHasher(f)
	n, err := io.Copy(h, r)
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		return 0, "", err
	}
	return n, h.Digest(), nil
}

func (s *Store) StatBlob(digest string) (int64, error) {
	st, err := os.Stat(s.blobPath(digest))
	if err != nil {
		return 0, err
	}
	return st.Size(), nil
}

func (s *Store) BlobExists(digest string) bool {
	_, err := os.Stat(s.blobPath(digest))
	return err == nil
}

func (s *Store) OpenBlob(digest string) (*os.File, int64, error) {
	f, err := os.Open(s.blobPath(digest))
	if err != nil {
		return nil, 0, err
	}
	st, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, 0, err
	}
	return f, st.Size(), nil
}

// RemoveBlob deletes a published blob file directly (used by reconciliations
// that already hold the database guarantee it is unreferenced).
func (s *Store) RemoveBlob(digest string) error {
	err := os.Remove(s.blobPath(digest))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

// Quarantine moves a blob into the run's quarantine dir. Crash-safe ordering:
// this happens *before* the DB row is deleted, so a crash here simply means
// the DB row remains and recovery restores (or reclaims) the file.
func (s *Store) Quarantine(runID, digest string) error {
	dst := s.quarantinePath(runID, digest)
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	if err := os.Rename(s.blobPath(digest), dst); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil // already moved on a resumed sweep
		}
		return err
	}
	return fsyncDir(filepath.Dir(dst))
}

// Restore moves a quarantined file back to its CAS location (crash recovery).
func (s *Store) Restore(runID, digest string) error {
	src := s.quarantinePath(runID, digest)
	dst := s.blobPath(digest)
	if _, err := os.Stat(src); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	if s.BlobExists(digest) {
		return os.Remove(src) // same digest already republished
	}
	if err := os.Rename(src, dst); err != nil {
		return err
	}
	return fsyncDir(filepath.Dir(dst))
}

// Purge destroys a quarantined blob after its DB delete is durable.
func (s *Store) Purge(runID, digest string) error {
	err := os.Remove(s.quarantinePath(runID, digest))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

// RemoveTemp deletes a staging file (successful finalize or abandoned upload).
func (s *Store) RemoveTemp(name string) error {
	err := os.Remove(s.TempPath(name))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

// ReplaceTemp atomically replaces staging file dst with src and removes src.
func (s *Store) ReplaceTemp(dst, src string) error {
	if err := os.Rename(s.TempPath(src), s.TempPath(dst)); err != nil {
		return err
	}
	return fsyncDir(s.tmpDir)
}

// ListTemp returns names of all files in the staging directory.
func (s *Store) ListTemp() ([]string, error) {
	entries, err := os.ReadDir(s.tmpDir)
	if err != nil {
		return nil, err
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() {
			names = append(names, e.Name())
		}
	}
	return names, nil
}

// ListBlobDigests returns all published blob digests found on disk.
func (s *Store) ListBlobDigests() ([]string, error) {
	var out []string
	err := filepath.WalkDir(s.blobsDir, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		if strings.HasPrefix(d.Name(), "sha256:") {
			out = append(out, d.Name())
		}
		return nil
	})
	return out, err
}

// ListQuarantine returns runID -> digests currently held in quarantine.
func (s *Store) ListQuarantine() (map[string][]string, error) {
	out := map[string][]string{}
	runs, err := os.ReadDir(s.quarantine)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return out, nil
		}
		return nil, err
	}
	for _, run := range runs {
		if !run.IsDir() {
			continue
		}
		entries, err := os.ReadDir(filepath.Join(s.quarantine, run.Name()))
		if err != nil {
			return nil, err
		}
		for _, e := range entries {
			if !e.IsDir() {
				out[run.Name()] = append(out[run.Name()], e.Name())
			}
		}
	}
	return out, nil
}

// PurgeQuarantineDir removes a run quarantine directory.
func (s *Store) PurgeQuarantineDir(runID string) error {
	return os.RemoveAll(filepath.Join(s.quarantine, filepath.Base(runID)))
}

// TempDir exposes the staging directory (configuration/diagnostics).
func (s *Store) TempDir() string { return s.tmpDir }

// VerifyBlobFile re-hashes a published blob and confirms it matches its
// content-address name. Used before adopting a stray file after recovery.
func (s *Store) VerifyBlobFile(digest string) (int64, error) {
	f, err := os.Open(s.blobPath(digest))
	if err != nil {
		return 0, err
	}
	defer f.Close()
	h := digestx.NewHasher(io.Discard)
	n, err := io.Copy(h, f)
	if err != nil {
		return 0, err
	}
	if h.Digest() != digest {
		return n, fmt.Errorf("%w: %s contains content hashing to %s",
			digestx.ErrDigestMismatch, digest, h.Digest())
	}
	return n, nil
}
