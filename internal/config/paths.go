package config

import (
	"os"
	"path/filepath"
	"strings"
)

// filepathEvalSymlinks resolves all symlinks and makes the path absolute.
func filepathEvalSymlinks(p string) (string, error) {
	return filepath.EvalSymlinks(p)
}

// pathWithin reports whether child is equal to root or strictly below root.
// Both arguments must already be absolute and symlink-free ("real") paths.
// The separator-aware comparison prevents /data/ev from matching /data/evidence.
func pathWithin(child, root string) bool {
	if child == root {
		return true
	}
	rel, err := filepath.Rel(root, child)
	if err != nil {
		return false
	}
	if rel == "." {
		return true
	}
	if rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) ||
		filepath.IsAbs(rel) {
		return false
	}
	return true
}
