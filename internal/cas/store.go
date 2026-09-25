// Package cas implements a content-addressed object store on the local
// filesystem. Objects are written to a temporary file first, their SHA-256
// is verified against the claimed digest, and only then are they atomically
// published with rename(2) — readers therefore never observe partial
// objects. The store root must be separate from any build working
// directory.
package cas

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"

	"localcache/api"
)

var (
	ErrNotFound       = errors.New("cas: object not found")
	ErrTooLarge       = errors.New("cas: object exceeds maximum size")
	ErrDigestMismatch = errors.New("cas: content does not match claimed digest")
	ErrInvalidDigest  = errors.New("cas: invalid digest (want 64 lowercase hex chars)")
)

// Store is a content-addressed blob store rooted at a directory.
//
// Layout:
//
//	<root>/objects/<sha256-hex>   published objects (read-only, complete)
//	<root>/tmp/upload-*           in-flight uploads, never served
type Store struct {
	root    string
	maxSize int64
}

// Open creates (if needed) the store layout under root and removes any
// leftover temporary uploads from a previous run, so an interrupted upload
// can never leak into the published namespace after a restart.
func Open(root string, maxSize int64) (*Store, error) {
	if maxSize <= 0 {
		return nil, fmt.Errorf("cas: max object size must be positive, got %d", maxSize)
	}
	for _, d := range []string{root, filepath.Join(root, "objects"), filepath.Join(root, "tmp")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, err
		}
	}
	s := &Store{root: root, maxSize: maxSize}
	if err := s.cleanTmp(); err != nil {
		return nil, fmt.Errorf("cas: cleaning tmp dir: %w", err)
	}
	return s, nil
}

// ValidateDigest checks that d is a lowercase hex SHA-256 digest.
func ValidateDigest(d string) error {
	if len(d) != 64 {
		return ErrInvalidDigest
	}
	for i := 0; i < len(d); i++ {
		c := d[i]
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return ErrInvalidDigest
		}
	}
	return nil
}

// DigestOf returns the SHA-256 hex digest of data.
func DigestOf(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// MaxSize returns the configured per-object size limit in bytes.
func (s *Store) MaxSize() int64 { return s.maxSize }

func (s *Store) objectPath(digest string) string {
	return filepath.Join(s.root, "objects", digest)
}

// Put stores the content read from r under digest. The content is streamed
// to a temporary file while its SHA-256 is computed; only if the hash
// matches digest and the size limit is respected is the file fsynced and
// atomically renamed into place. Concurrent Puts of the same digest are
// safe: each writes a distinct temp file and renames identical bytes to the
// same target.
func (s *Store) Put(digest string, r io.Reader) (int64, error) {
	if err := ValidateDigest(digest); err != nil {
		return 0, err
	}
	if size, ok := s.Has(digest); ok {
		// Already published; drain the body (so HTTP keep-alive works) but
		// keep the existing object untouched.
		io.Copy(io.Discard, io.LimitReader(r, s.maxSize+1))
		return size, nil
	}

	tmp, err := os.CreateTemp(filepath.Join(s.root, "tmp"), "upload-*")
	if err != nil {
		return 0, err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op once renamed

	h := sha256.New()
	n, copyErr := io.Copy(io.MultiWriter(tmp, h), io.LimitReader(r, s.maxSize+1))
	if n > s.maxSize {
		tmp.Close()
		return 0, fmt.Errorf("%w: limit is %d bytes", ErrTooLarge, s.maxSize)
	}
	if copyErr != nil {
		tmp.Close()
		return 0, fmt.Errorf("cas: reading upload: %w", copyErr)
	}
	if got := hex.EncodeToString(h.Sum(nil)); got != digest {
		tmp.Close()
		return 0, fmt.Errorf("%w: got %s", ErrDigestMismatch, got)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return 0, err
	}
	if err := tmp.Close(); err != nil {
		return 0, err
	}
	if err := os.Chmod(tmpName, 0o444); err != nil {
		return 0, err
	}
	if err := os.Rename(tmpName, s.objectPath(digest)); err != nil {
		return 0, err
	}
	return n, nil
}

// Get opens a published object for reading and reports its size.
func (s *Store) Get(digest string) (io.ReadCloser, int64, error) {
	if err := ValidateDigest(digest); err != nil {
		return nil, 0, err
	}
	f, err := os.Open(s.objectPath(digest))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, 0, ErrNotFound
		}
		return nil, 0, err
	}
	st, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, 0, err
	}
	return f, st.Size(), nil
}

// Has reports whether a complete object is published under digest.
func (s *Store) Has(digest string) (int64, bool) {
	if ValidateDigest(digest) != nil {
		return 0, false
	}
	st, err := os.Stat(s.objectPath(digest))
	if err != nil {
		return 0, false
	}
	return st.Size(), true
}

// Stats returns the number of published objects and their total size.
func (s *Store) Stats() (objects, bytes int64) {
	entries, err := os.ReadDir(filepath.Join(s.root, "objects"))
	if err != nil {
		return 0, 0
	}
	for _, e := range entries {
		if e.IsDir() || ValidateDigest(e.Name()) != nil {
			continue
		}
		if st, err := e.Info(); err == nil {
			objects++
			bytes += st.Size()
		}
	}
	return objects, bytes
}

func (s *Store) cleanTmp() error {
	entries, err := os.ReadDir(filepath.Join(s.root, "tmp"))
	if err != nil {
		return err
	}
	for _, e := range entries {
		if err := os.RemoveAll(filepath.Join(s.root, "tmp", e.Name())); err != nil {
			return err
		}
	}
	return nil
}

// Fsck re-hashes every published object and reports anything inconsistent,
// so a damaged cache is diagnosable: corrupt objects (content no longer
// matches the address), leftover temporary uploads, and foreign entries.
func (s *Store) Fsck() (*api.FsckReport, error) {
	rep := &api.FsckReport{}

	objDir := filepath.Join(s.root, "objects")
	entries, err := os.ReadDir(objDir)
	if err != nil {
		return nil, err
	}
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || ValidateDigest(name) != nil {
			rep.Unknown = append(rep.Unknown, filepath.Join("objects", name))
			continue
		}
		f, err := os.Open(filepath.Join(objDir, name))
		if err != nil {
			return nil, err
		}
		h := sha256.New()
		n, copyErr := io.Copy(h, f)
		f.Close()
		if copyErr != nil {
			return nil, copyErr
		}
		rep.ObjectsChecked++
		rep.BytesChecked += n
		if got := hex.EncodeToString(h.Sum(nil)); got != name {
			rep.Corrupt = append(rep.Corrupt, fmt.Sprintf("%s (content hashes to %s)", name, got))
		}
	}

	tmpEntries, err := os.ReadDir(filepath.Join(s.root, "tmp"))
	if err != nil {
		return nil, err
	}
	for _, e := range tmpEntries {
		rep.LeftoverTmp = append(rep.LeftoverTmp, filepath.Join("tmp", e.Name()))
	}

	rootEntries, err := os.ReadDir(s.root)
	if err != nil {
		return nil, err
	}
	for _, e := range rootEntries {
		switch e.Name() {
		case "objects", "tmp", "ac": // known layout
		default:
			rep.Unknown = append(rep.Unknown, e.Name())
		}
	}

	sort.Strings(rep.Corrupt)
	sort.Strings(rep.LeftoverTmp)
	sort.Strings(rep.Unknown)
	rep.OK = len(rep.Corrupt) == 0 && len(rep.LeftoverTmp) == 0 && len(rep.Unknown) == 0
	return rep, nil
}
