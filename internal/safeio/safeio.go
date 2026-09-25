// Package safeio contains path containment helpers. The service only ever
// touches files inside (a) a declared project directory or (b) its own
// cache/work/state roots, and every declared relative path is checked.
package safeio

import (
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// ErrEscapesBase is returned when a relative path resolves outside its base.
var ErrEscapesBase = errors.New("path escapes its base directory")

// ResolveWithinSafeLexical validates that rel is a non-empty relative, clean
// path that does not escape its (notional) base. It touches no filesystem,
// so it also works for purely static validation. Returns the cleaned path.
func ResolveWithinSafeLexical(rel string) (string, error) {
	if rel == "" || filepath.IsAbs(rel) {
		return "", ErrEscapesBase
	}
	clean := filepath.Clean(rel)
	if clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return "", ErrEscapesBase
	}
	return clean, nil
}

// ResolveWithin joins rel onto base and guarantees the result cannot refer to
// anything outside base, including through symlinks when the target exists.
func ResolveWithin(base, rel string) (string, error) {
	if rel == "" || filepath.IsAbs(rel) {
		return "", ErrEscapesBase
	}
	clean := filepath.Clean(rel)
	if clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return "", ErrEscapesBase
	}
	full := filepath.Join(base, clean)

	baseReal, err := filepath.EvalSymlinks(base)
	if err != nil {
		// Base must exist; otherwise containment cannot be established.
		return "", err
	}
	targetReal, err := filepath.EvalSymlinks(full)
	switch {
	case err == nil:
		relTo, err := filepath.Rel(baseReal, targetReal)
		if err != nil || relTo == ".." || strings.HasPrefix(relTo, ".."+string(filepath.Separator)) {
			return "", ErrEscapesBase
		}
		return targetReal, nil
	case os.IsNotExist(err):
		// Target does not exist yet: validate the existing parent chain and
		// fall back to the lexical form.
		parent := filepath.Dir(full)
		if parentReal, perr := filepath.EvalSymlinks(parent); perr == nil {
			relTo, err := filepath.Rel(baseReal, parentReal)
			if err != nil || relTo == ".." || strings.HasPrefix(relTo, ".."+string(filepath.Separator)) {
				return "", ErrEscapesBase
			}
		} else if !os.IsNotExist(perr) {
			return "", perr
		}
		return full, nil
	default:
		return "", err
	}
}

// CopyFile copies a single regular file, creating parent directories and
// applying the supplied mode to the destination.
func CopyFile(dst, src string, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, mode)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}
