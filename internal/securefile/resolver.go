package securefile

import (
	"fmt"
	"os"
	"path/filepath"
)

// Resolver opens evidence files only from a fixed set of whitelisted
// directories, rejecting path traversal ("../../etc/passwd"), absolute
// escapes and symlink redirection.
type Resolver struct {
	roots []string
}

// NewResolver builds a Resolver. roots must be absolute symlink-free paths;
// callers (config) have already validated them.
func NewResolver(roots []string) *Resolver {
	cp := make([]string, len(roots))
	copy(cp, roots)
	return &Resolver{roots: cp}
}

// Roots returns the configured whitelist roots.
func (r *Resolver) Roots() []string {
	cp := make([]string, len(r.roots))
	copy(cp, r.roots)
	return cp
}

// Resolve validates requestedPath and opens it read-only.
//
// Validation order: extension -> absolute/real resolution of the requested
// path -> whitelist containment -> regular file -> open (O_NOFOLLOW on Linux)
// -> re-validate the descriptor's real path via /proc. The post-open check
// closes the symlink swap TOCTOU window.
func (r *Resolver) Resolve(requestedPath string) (*File, error) {
	if !HasRawExt(requestedPath) {
		return nil, ErrUnsupportedType
	}
	abs, err := filepath.Abs(requestedPath)
	if err != nil {
		return nil, err
	}
	// EvalSymlinks rejects a missing final component and resolves every
	// symlink, including in intermediate directories.
	real, err := filepath.EvalSymlinks(abs)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, fmt.Errorf("%w: %s", os.ErrNotExist, abs)
		}
		return nil, err
	}
	if !r.withinAny(real) {
		return nil, fmt.Errorf("%w: %s", ErrOutsideWhitelist, real)
	}
	f, err := openHardened(real)
	if err != nil {
		return nil, err
	}
	// Verify the descriptor itself, defeating a swap between EvalSymlinks and
	// open (an intermediate directory replaced by a symlink in that window).
	id, err := fdIdentity(f)
	if err != nil {
		_ = f.Close()
		return nil, err
	}
	id.RealPath = real
	// Independently ask the kernel where the descriptor actually points.
	if fdReal, err := fdRealPath(f); err == nil && fdReal != "" && fdReal != real {
		_ = f.Close()
		return nil, ErrSymlinkEscape
	}
	if !r.withinAny(real) {
		_ = f.Close()
		return nil, fmt.Errorf("%w: %s", ErrSymlinkEscape, real)
	}
	info := modeFromFd(id.Mode)
	if info != nil {
		_ = f.Close()
		return nil, info
	}
	return &File{File: f, identity: id}, nil
}

func (r *Resolver) withinAny(realPath string) bool {
	for _, root := range r.roots {
		if within(realPath, root) {
			return true
		}
	}
	return false
}

func modeFromFd(mode os.FileMode) error {
	if mode.IsDir() {
		return ErrNotRegularFile
	}
	if !mode.IsRegular() {
		return ErrNotRegularFile
	}
	return nil
}
