// Package storage manages on-disk layout for uploaded PNG assets and rendered
// frame output. All user-derived names are validated: nothing may escape its
// configured root directory.
package storage

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

var (
	ErrUnsafePath = errors.New("path escapes storage root")
	ErrBadName    = errors.New("invalid file name")
)

// SafeJoin resolves name inside root and guarantees the result stays there.
// name must be a single path component (no separators, no "..").
func SafeJoin(root, name string) (string, error) {
	if name == "" || name == "." || name == ".." ||
		strings.ContainsAny(name, "/\\") ||
		strings.HasPrefix(name, ".") {
		return "", fmt.Errorf("%w: %q", ErrBadName, name)
	}
	clean := filepath.Clean(filepath.Join(root, name))
	rootAbs, err := filepath.Abs(root)
	if err != nil {
		return "", err
	}
	cleanAbs, err := filepath.Abs(clean)
	if err != nil {
		return "", err
	}
	if cleanAbs != filepath.Join(rootAbs, name) {
		return "", fmt.Errorf("%w: %q", ErrUnsafePath, name)
	}
	return cleanAbs, nil
}

// EnsureDir creates the directory if missing.
func EnsureDir(dir string) error {
	return os.MkdirAll(dir, 0o755)
}

// AssetPath returns the content-addressed path for an asset blob.
func AssetPath(assetsDir, sha256hex string) string {
	// Two-level sharding keeps directories small: aa/bb/<full>.png
	return filepath.Join(assetsDir, sha256hex[0:2], sha256hex[2:4], sha256hex+".png")
}

// SaveReader writes r to a temp file in the same directory as dst, fsyncs it,
// then atomically renames over dst. Callers that need transaction-coupled
// semantics write the file first and commit the DB row only after this
// returns: a crash in between leaves an unreferenced blob that is harmless.
func SaveReader(dst string, r io.Reader) (int64, error) {
	dir := filepath.Dir(dst)
	if err := EnsureDir(dir); err != nil {
		return 0, err
	}
	f, err := os.CreateTemp(dir, ".tmp-*")
	if err != nil {
		return 0, err
	}
	tmpName := f.Name()
	n, err := io.Copy(f, r)
	if err != nil {
		_ = f.Close()
		_ = os.Remove(tmpName)
		return 0, err
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		_ = os.Remove(tmpName)
		return 0, err
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(tmpName)
		return 0, err
	}
	if err := os.Rename(tmpName, dst); err != nil {
		_ = os.Remove(tmpName)
		return 0, err
	}
	return n, nil
}

// AtomicWriteFile is SaveReader for an in-memory payload.
func AtomicWriteFile(dst string, data []byte) error {
	_, err := SaveReader(dst, bytesReader(data))
	return err
}

func bytesReader(b []byte) io.Reader { return bytes.NewReader(b) }

// SHA256File returns the lowercase hex SHA-256 of the file at path.
func SHA256File(path string) (string, int64, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(h.Sum(nil)), n, nil
}

// RemoveIfExists removes a file, ignoring "not found".
func RemoveIfExists(path string) error {
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}
