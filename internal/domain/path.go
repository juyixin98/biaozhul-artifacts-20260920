package domain

import (
	"path"
	"strconv"
	"strings"
)

// ValidateRelPath enforces that an asset reference stays inside the
// project's asset root: no absolute paths, no volume/Windows roots, no
// ".." traversal, no empty or directory-only segments. Any path escaping
// the root is rejected before the filesystem is touched.
func ValidateRelPath(p string) error {
	if p == "" {
		return &PathError{Path: p, Msg: "empty path"}
	}
	if strings.ContainsRune(p, 0) {
		return &PathError{Path: p, Msg: "null byte in path"}
	}
	if path.IsAbs(p) || strings.HasPrefix(p, "/") {
		return &PathError{Path: p, Msg: "absolute paths are not allowed"}
	}
	if strings.HasPrefix(p, "\\") || strings.Contains(p, ":\\") {
		return &PathError{Path: p, Msg: "windows-style roots are not allowed"}
	}
	clean := path.Clean(p)
	if clean == ".." || strings.HasPrefix(clean, "../") {
		return &PathError{Path: p, Msg: "path escapes the asset root"}
	}
	if clean == "." || clean == "/" {
		return &PathError{Path: p, Msg: "path must name a file"}
	}
	for _, seg := range strings.Split(p, "/") {
		switch seg {
		case "":
			return &PathError{Path: p, Msg: "empty path segment"}
		case ".":
			return &PathError{Path: p, Msg: "current-directory segments are not allowed"}
		case "..":
			return &PathError{Path: p, Msg: "parent-directory segments are not allowed"}
		}
	}
	if !strings.HasSuffix(strings.ToLower(clean), ".png") {
		return &PathError{Path: p, Msg: "only .png assets are supported"}
	}
	return nil
}

// PathError describes a rejected asset path.
type PathError struct {
	Path string
	Msg  string
}

func (e *PathError) Error() string {
	return "invalid asset path " + strconv.Quote(e.Path) + ": " + e.Msg
}
