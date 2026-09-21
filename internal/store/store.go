// Package store manages the on-disk, content-addressed blob store.
//
// Layout under DataDir:
//
//	objects/<xx>/<yy>/<sha256-hex>  committed blobs (path derived from digest)
//	tmp/<random>                    staging files, renamed into objects/ on commit
//	graveyard/<digest>-<unixnano>   blobs detached by the sweeper, unlinked after
//	                                the DB transaction that removed their row
//
// Nothing ever mutates a committed blob in place: a digest identifies exact
// bytes, and publishing is a single rename, so a half-written file can never
// appear at a committed path.
package store

import (
	"crypto/rand"
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

type FileStore struct {
	root      string
	objects   string
	tmp       string
	graveyard string
}

func New(root string) (*FileStore, error) {
	fs := &FileStore{
		root:      root,
		objects:   filepath.Join(root, "objects"),
		tmp:       filepath.Join(root, "tmp"),
		graveyard: filepath.Join(root, "graveyard"),
	}
	for _, d := range []string{fs.objects, fs.tmp, fs.graveyard} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, err
		}
	}
	return fs, nil
}

func (f *FileStore) Root() string { return f.root }

// ObjectPath returns the committed path for a digest.
func (f *FileStore) ObjectPath(digest string) string {
	return filepath.Join(f.objects, digest[0:2], digest[2:4], digest)
}

// TempPath returns the staging directory path.
func (f *FileStore) TempDir() string { return f.tmp }

// NewStagingFile creates a fresh staging file.
func (f *FileStore) NewStagingFile() (*os.File, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return nil, err
	}
	name := hex.EncodeToString(b[:])
	return os.OpenFile(filepath.Join(f.tmp, name), os.O_RDWR|os.O_CREATE|os.O_EXCL, 0o644)
}

// StagingFromReader copies r into a new staging file and fsyncs it, returning
// the path, byte count and hex SHA-256 of what was written.
func (f *FileStore) StagingFromReader(r io.Reader) (path string, n int64, digest string, err error) {
	file, err := f.NewStagingFile()
	if err != nil {
		return "", 0, "", err
	}
	path = file.Name()
	hasher := sha256.New()
	n, err = io.Copy(io.MultiWriter(file, hasher), r)
	if err != nil {
		_ = file.Close()
		_ = os.Remove(path)
		return "", 0, "", err
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		_ = os.Remove(path)
		return "", 0, "", err
	}
	if err := file.Close(); err != nil {
		_ = os.Remove(path)
		return "", 0, "", err
	}
	return path, n, hex.EncodeToString(hasher.Sum(nil)), nil
}

// CommitStaging moves a staging file to the committed path for digest. It is
// idempotent when a same-sized file already occupies the path.
func (f *FileStore) CommitStaging(staging, digest string, size int64) error {
	dst := f.ObjectPath(digest)
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	if info, err := os.Stat(dst); err == nil {
		if info.Size() != size {
			return fmt.Errorf("object %s exists with size %d, want %d", digest, info.Size(), size)
		}
		return os.Remove(staging)
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := os.Rename(staging, dst); err != nil {
		return err
	}
	// Best-effort durability for the directory entry.
	dir, err := os.Open(filepath.Dir(dst))
	if err == nil {
		_ = dir.Sync()
		_ = dir.Close()
	}
	return nil
}

// OpenObject opens a committed blob.
func (f *FileStore) OpenObject(digest string) (*os.File, error) {
	return os.Open(f.ObjectPath(digest))
}

// ObjectExists reports whether a committed blob of the given size is present.
func (f *FileStore) ObjectExists(digest string, size int64) bool {
	info, err := os.Stat(f.ObjectPath(digest))
	return err == nil && info.Size() == size
}

// RemoveStaging deletes an unused staging file (missing is fine).
func (f *FileStore) RemoveStaging(path string) { _ = os.Remove(path) }

// Quarantine moves a committed blob into the graveyard. Returns true when a
// file was actually moved.
func (f *FileStore) Quarantine(digest string) (bool, string, error) {
	src := f.ObjectPath(digest)
	if _, err := os.Stat(src); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return false, "", nil
		}
		return false, "", err
	}
	gst := filepath.Join(f.graveyard, fmt.Sprintf("%s-%d", digest, randSuffix()))
	if err := os.Rename(src, gst); err != nil {
		return false, "", err
	}
	return true, gst, nil
}

// PurgeGraveyard unlinks every file in the graveyard. Called at startup: a file
// only reaches the graveyard inside a committed transaction, so its row is
// already gone and the file is safe to remove.
func (f *FileStore) PurgeGraveyard() error {
	entries, err := os.ReadDir(f.graveyard)
	if err != nil {
		return err
	}
	for _, e := range entries {
		_ = os.Remove(filepath.Join(f.graveyard, e.Name()))
	}
	return nil
}

// PurgeStaging removes leftover staging files. Safe at startup because no
// request handlers are running yet.
func (f *FileStore) PurgeStaging() error {
	entries, err := os.ReadDir(f.tmp)
	if err != nil {
		return err
	}
	for _, e := range entries {
		_ = os.Remove(filepath.Join(f.tmp, e.Name()))
	}
	return nil
}

// OrphanObjects returns the digests of committed files that have no database
// row. Such files can only exist after a crash between the rename and the row
// commit (the row insert rolled back), so they are safe to delete.
func (f *FileStore) OrphanObjects(known func(digest string) (knownObj bool, err error)) ([]string, error) {
	var orphans []string
	err := filepath.WalkDir(f.objects, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		digest := filepath.Base(path)
		ok, err := known(digest)
		if err != nil {
			return err
		}
		if !ok {
			orphans = append(orphans, digest)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	sort.Strings(orphans)
	return orphans, nil
}

// RemoveObject unlinks a committed blob (used for orphans).
func (f *FileStore) RemoveObject(digest string) error {
	return os.Remove(f.ObjectPath(digest))
}

// ValidateDigest checks a lowercase hex SHA-256 string.
func ValidateDigest(digest string) bool {
	if len(digest) != 64 {
		return false
	}
	_, err := hex.DecodeString(digest)
	return err == nil && strings.ToLower(digest) == digest
}

func randSuffix() int64 {
	var b [8]byte
	_, _ = rand.Read(b[:])
	var v int64
	for i := 0; i < 8; i++ {
		v = v<<8 | int64(b[i])
	}
	if v < 0 {
		v = -v
	}
	return v
}
