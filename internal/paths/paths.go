// Package paths resolves and validates patch target paths against a work
// directory root. Paths must stay inside the root after lexical cleaning and
// after resolving every symlink component, including the final component when
// it already exists.
package paths

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
)

const CodeUnsafePath = "unsafe_path"

// Error describes a rejected path.
type Error struct {
	Path   string
	Reason string
}

func (e *Error) Error() string {
	return CodeUnsafePath + ": " + e.Path + ": " + e.Reason
}

// Resolve validates rel against root and returns the absolute target path.
// The returned path is lexical-cleaned but symlinks along the way are only
// inspected, never created.
func Resolve(root, rel string) (string, error) {
	if rel == "" {
		return "", &Error{Path: rel, Reason: "empty path"}
	}
	if strings.ContainsRune(rel, 0) {
		return "", &Error{Path: rel, Reason: "path contains a NUL byte"}
	}
	if filepath.IsAbs(rel) {
		return "", &Error{Path: rel, Reason: "absolute paths are not allowed"}
	}
	clean := filepath.Clean(filepath.FromSlash(rel))
	if clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return "", &Error{Path: rel, Reason: "path escapes the work directory (..)"}
	}
	if clean == "." {
		return "", &Error{Path: rel, Reason: "path resolves to the work directory root"}
	}

	absRoot, err := filepath.Abs(root)
	if err != nil {
		return "", err
	}
	target := filepath.Join(absRoot, clean)

	// Walk each existing component and ensure that symlink resolution never
	// leaves absRoot. Missing components are fine (files being created).
	cur := absRoot
	parts := splitComponents(target[len(absRoot):])
	for _, p := range parts {
		cur = filepath.Join(cur, p)
		fi, err := os.Lstat(cur)
		if err != nil {
			if os.IsNotExist(err) {
				continue
			}
			return "", err
		}
		if fi.Mode()&os.ModeSymlink == 0 {
			continue
		}
		dest, err := os.Readlink(cur)
		if err != nil {
			return "", err
		}
		resolved := dest
		if !filepath.IsAbs(resolved) {
			resolved = filepath.Join(filepath.Dir(cur), dest)
		}
		resolved = filepath.Clean(resolved)
		if !within(resolved, absRoot) {
			return "", &Error{Path: rel, Reason: "symlink escapes the work directory: " + dest}
		}
		// The symlink points inside the root; continue the walk from the
		// link target so that the target's own symlinks are checked too.
		cur = resolved
	}
	return target, nil
}

func within(p, root string) bool {
	if p == root {
		return true
	}
	return strings.HasPrefix(p, root+string(filepath.Separator))
}

func splitComponents(rel string) []string {
	rel = strings.TrimPrefix(filepath.ToSlash(rel), "/")
	if rel == "" {
		return nil
	}
	return strings.Split(rel, "/")
}

// IsNotExist reports whether an error means the path does not exist.
func IsNotExist(err error) bool { return errors.Is(err, os.ErrNotExist) }
