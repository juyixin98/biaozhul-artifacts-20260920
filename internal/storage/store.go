// Package storage manages on-disk content-addressed asset blobs, render
// outputs and crash-safe file publication.
package storage

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

// Store is rooted at one data directory:
//
//	root/
//	  blobs/<sha256[0:2]>/<sha256>          uploaded PNGs (immutable)
//	  outputs/<jobID>/frame_<n>.png          rendered frames
//	  outputs/<jobID>/summary.json           frame manifest
//	  tmp/                                   staging files, renamed atomically
type Store struct {
	Root string
}

// ErrNotFound means the blob/file does not exist.
var ErrNotFound = errors.New("file not found")

// New creates the directory skeleton.
func New(root string) (*Store, error) {
	root, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	for _, sub := range []string{"blobs", "outputs", "tmp"} {
		if err := os.MkdirAll(filepath.Join(root, sub), 0o755); err != nil {
			return nil, fmt.Errorf("create %s: %w", sub, err)
		}
	}
	return &Store{Root: root}, nil
}

// BlobPath returns the absolute path of a content-addressed blob.
func (s *Store) BlobPath(sha256hex string) string {
	return filepath.Join(s.Root, "blobs", sha256hex[0:2], sha256hex)
}

// HasBlob reports whether a blob exists on disk.
func (s *Store) HasBlob(sha256hex string) bool {
	if len(sha256hex) != 64 {
		return false
	}
	_, err := os.Stat(s.BlobPath(sha256hex))
	return err == nil
}

// PutBlob streams a PNG into the content-addressed store. It returns the
// digest and number of bytes. The upload is written to a temp file and
// renamed into place, so a crash never leaves a half-written blob visible.
// If the same digest already exists the temp file is discarded (idempotent).
func (s *Store) PutBlob(r io.Reader) (sha256hex string, n int64, err error) {
	tmp, err := os.CreateTemp(filepath.Join(s.Root, "tmp"), "blob-*")
	if err != nil {
		return "", 0, err
	}
	tmpName := tmp.Name()
	defer func() {
		tmp.Close()
		os.Remove(tmpName) // no-op after successful rename
	}()

	h := sha256.New()
	n, err = io.Copy(io.MultiWriter(tmp, h), r)
	if err != nil {
		return "", 0, fmt.Errorf("write blob: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		return "", 0, err
	}
	if err := tmp.Close(); err != nil {
		return "", 0, err
	}

	digest := hex.EncodeToString(h.Sum(nil))
	dst := s.BlobPath(digest)
	if _, err := os.Stat(dst); err == nil {
		return digest, n, nil // already present
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return "", 0, err
	}
	if err := os.Rename(tmpName, dst); err != nil {
		return "", 0, err
	}
	return digest, n, nil
}

// OpenBlob opens a verified blob for reading.
func (s *Store) OpenBlob(sha256hex string) (*os.File, error) {
	f, err := os.Open(s.BlobPath(sha256hex))
	if errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("%w: blob %s", ErrNotFound, sha256hex[:12])
	}
	return f, err
}

// VerifyBlob re-hashes a blob and compares it to the expected digest.
// Workers call this before rendering so a missing/corrupt layer fails the
// frame rather than producing a wrong image.
func (s *Store) VerifyBlob(sha256hex string) error {
	f, err := s.OpenBlob(sha256hex)
	if err != nil {
		return err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return err
	}
	if got := hex.EncodeToString(h.Sum(nil)); got != sha256hex {
		return fmt.Errorf("blob digest mismatch: db says %s, disk is %s", sha256hex[:12], got[:12])
	}
	return nil
}

// JobDir is the output directory of one job.
func (s *Store) JobDir(jobID string) string {
	return filepath.Join(s.Root, "outputs", jobID)
}

// FramePath is the final frame output path.
func (s *Store) FramePath(jobID string, frameNo int) string {
	return filepath.Join(s.JobDir(jobID), fmt.Sprintf("frame_%06d.png", frameNo))
}

// SummaryPath is the final summary file path.
func (s *Store) SummaryPath(jobID string) string {
	return filepath.Join(s.JobDir(jobID), "summary.json")
}

// PublishFrame atomically installs rendered bytes at the frame path.
//
// Ordering is important: bytes are fsynced to a temp file in the SAME
// directory and renamed, so the final path appears only when the file is
// durable. The caller commits the DB row afterwards; if that commit is
// lost, RecoverOrphanedOutputs removes the dangling file on restart.
func (s *Store) PublishFrame(jobID string, frameNo int, data []byte) (sha256hex string, size int64, err error) {
	dir := s.JobDir(jobID)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", 0, err
	}
	digest, err := publishAtomic(s.FramePath(jobID, frameNo), data)
	if err != nil {
		return "", 0, err
	}
	return digest, int64(len(data)), nil
}

// PublishSummary writes summary.json atomically, only once the job is
// terminal. fsync of the directory guarantees the rename is durable.
func (s *Store) PublishSummary(jobID string, data []byte) error {
	if err := os.MkdirAll(s.JobDir(jobID), 0o755); err != nil {
		return err
	}
	_, err := publishAtomic(s.SummaryPath(jobID), data)
	return err
}

// FrameExists reports whether a frame file is present with the expected
// digest and size; used during crash recovery.
func (s *Store) FrameExists(jobID string, frameNo int, expectedSHA string, expectedSize int64) bool {
	f, err := os.Open(s.FramePath(jobID, frameNo))
	if err != nil {
		return false
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return false
	}
	if hex.EncodeToString(h.Sum(nil)) != expectedSHA {
		return false
	}
	fi, err := f.Stat()
	if err != nil {
		return false
	}
	return fi.Size() == expectedSize
}

// RemoveFrame deletes a frame output (used when cleaning an orphaned or
// superseded file). Missing file is not an error.
func (s *Store) RemoveFrame(jobID string, frameNo int) error {
	err := os.Remove(s.FramePath(jobID, frameNo))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

// CleanupJobDir removes every output of a job (used for canceled jobs and
// tests).
func (s *Store) CleanupJobDir(jobID string) error {
	if strings.ContainsAny(jobID, "/\\") {
		return fmt.Errorf("bad job id %q", jobID)
	}
	err := os.RemoveAll(s.JobDir(jobID))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

// SweepTemp removes staging files left by a crash mid-publication.
func (s *Store) SweepTemp() error {
	entries, err := os.ReadDir(filepath.Join(s.Root, "tmp"))
	if err != nil {
		return err
	}
	for _, e := range entries {
		_ = os.Remove(filepath.Join(s.Root, "tmp", e.Name()))
	}
	return nil
}

// publishAtomic writes data to a sibling temp file, fsyncs it, renames it
// over dst and fsyncs the containing directory.
func publishAtomic(dst string, data []byte) (string, error) {
	dir := filepath.Dir(dst)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	tmp, err := os.CreateTemp(dir, ".tmp-*")
	if err != nil {
		return "", err
	}
	tmpName := tmp.Name()
	cleanup := true
	defer func() {
		if cleanup {
			tmp.Close()
			os.Remove(tmpName)
		}
	}()
	h := sha256.New()
	if _, err := tmp.Write(data); err != nil {
		return "", err
	}
	if _, err := h.Write(data); err != nil {
		return "", err
	}
	if err := tmp.Sync(); err != nil {
		return "", err
	}
	if err := tmp.Close(); err != nil {
		return "", err
	}
	if err := os.Rename(tmpName, dst); err != nil {
		return "", err
	}
	if df, err := os.Open(dir); err == nil {
		_ = df.Sync()
		_ = df.Close()
	}
	cleanup = false
	return hex.EncodeToString(h.Sum(nil)), nil
}
