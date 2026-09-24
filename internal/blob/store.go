// Package blob implements a content-addressable, per-environment blob store.
//
// Blobs are addressed only by their immutable sha256 digest
// ("sha256:<64 hex>") — never by floating tags. Every write goes to a temp
// file, is hashed while streaming, fsynced, and atomically renamed into
// place, so a reader never observes a partial blob. Copies between
// environment namespaces recompute the digest of the bytes actually written
// and fail unless it matches the expected digest exactly.
package blob

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

const DigestPrefix = "sha256:"

// ValidateDigest enforces the immutable digest form; tags are rejected by
// construction because no API accepts anything else.
func ValidateDigest(d string) error {
	if !strings.HasPrefix(d, DigestPrefix) {
		return fmt.Errorf("digest %q must start with %q", d, DigestPrefix)
	}
	hexPart := strings.TrimPrefix(d, DigestPrefix)
	if len(hexPart) != 64 {
		return fmt.Errorf("digest %q must have 64 hex chars", d)
	}
	if _, err := hex.DecodeString(hexPart); err != nil {
		return fmt.Errorf("digest %q is not valid hex: %w", d, err)
	}
	return nil
}

type Store struct {
	root string
}

func New(root string) (*Store, error) {
	if err := os.MkdirAll(root, 0o755); err != nil {
		return nil, fmt.Errorf("create blob root: %w", err)
	}
	return &Store{root: root}, nil
}

func (s *Store) dir(env string) string { return filepath.Join(s.root, env) }

// Path returns the on-disk location of digest in env's namespace.
// Callers must have validated digest first.
func (s *Store) Path(env, digest string) string {
	return filepath.Join(s.dir(env), strings.TrimPrefix(digest, DigestPrefix))
}

func (s *Store) Exists(env, digest string) bool {
	if err := ValidateDigest(digest); err != nil {
		return false
	}
	st, err := os.Stat(s.Path(env, digest))
	return err == nil && st.Mode().IsRegular()
}

// Put streams r into env's namespace and returns the digest of the bytes
// actually written.
func (s *Store) Put(env string, r io.Reader) (digest string, size int64, err error) {
	if err := os.MkdirAll(s.dir(env), 0o755); err != nil {
		return "", 0, fmt.Errorf("create env namespace: %w", err)
	}
	tmp, err := os.CreateTemp(s.dir(env), ".tmp-*")
	if err != nil {
		return "", 0, fmt.Errorf("create temp blob: %w", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op after successful rename

	h := sha256.New()
	size, err = io.Copy(io.MultiWriter(tmp, h), r)
	if err != nil {
		tmp.Close()
		return "", 0, fmt.Errorf("write blob: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return "", 0, fmt.Errorf("fsync blob: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return "", 0, fmt.Errorf("close blob: %w", err)
	}
	digest = DigestPrefix + hex.EncodeToString(h.Sum(nil))
	final := s.Path(env, digest)
	if _, err := os.Stat(final); err == nil {
		return digest, size, nil // identical content already stored
	}
	if err := os.Rename(tmpName, final); err != nil {
		return "", 0, fmt.Errorf("commit blob: %w", err)
	}
	return digest, size, nil
}

// Copy duplicates digest from srcEnv's namespace into dstEnv's namespace,
// recomputing the sha256 of the copied bytes and failing unless it equals
// the expected digest. A partial or corrupt copy is never left behind.
func (s *Store) Copy(srcEnv, dstEnv, digest string) (size int64, err error) {
	if err := ValidateDigest(digest); err != nil {
		return 0, err
	}
	src := s.Path(srcEnv, digest)
	if _, err := os.Stat(src); err != nil {
		return 0, fmt.Errorf("source blob %s missing in env %q", digest, srcEnv)
	}
	if s.Exists(dstEnv, digest) {
		// Already present; verify what is there before trusting it.
		return s.Verify(dstEnv, digest)
	}
	if err := os.MkdirAll(s.dir(dstEnv), 0o755); err != nil {
		return 0, fmt.Errorf("create env namespace: %w", err)
	}

	in, err := os.Open(src)
	if err != nil {
		return 0, fmt.Errorf("open source blob: %w", err)
	}
	defer in.Close()

	tmp, err := os.CreateTemp(s.dir(dstEnv), ".tmp-copy-*")
	if err != nil {
		return 0, fmt.Errorf("create temp copy: %w", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // removes the partial copy on any failure

	h := sha256.New()
	size, err = io.Copy(io.MultiWriter(tmp, h), in)
	if err != nil {
		tmp.Close()
		return 0, fmt.Errorf("copy blob: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return 0, fmt.Errorf("fsync copy: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return 0, fmt.Errorf("close copy: %w", err)
	}
	got := DigestPrefix + hex.EncodeToString(h.Sum(nil))
	if got != digest {
		return 0, fmt.Errorf("copy verification failed: wrote digest %s, expected %s", got, digest)
	}
	if err := os.Rename(tmpName, s.Path(dstEnv, digest)); err != nil {
		return 0, fmt.Errorf("commit copy: %w", err)
	}
	return size, nil
}

// Verify recomputes the sha256 of the stored blob and compares it to the
// digest. This is the completeness check used before a rollback is allowed.
func (s *Store) Verify(env, digest string) (size int64, err error) {
	if err := ValidateDigest(digest); err != nil {
		return 0, err
	}
	f, err := os.Open(s.Path(env, digest))
	if err != nil {
		return 0, fmt.Errorf("blob %s missing in env %q", digest, env)
	}
	defer f.Close()
	h := sha256.New()
	size, err = io.Copy(h, f)
	if err != nil {
		return 0, fmt.Errorf("read blob: %w", err)
	}
	got := DigestPrefix + hex.EncodeToString(h.Sum(nil))
	if got != digest {
		return 0, fmt.Errorf("integrity check failed: stored digest %s, expected %s", got, digest)
	}
	return size, nil
}

// Remove deletes a blob; used by tests and retention tooling.
func (s *Store) Remove(env, digest string) error {
	if err := ValidateDigest(digest); err != nil {
		return err
	}
	err := os.Remove(s.Path(env, digest))
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}
