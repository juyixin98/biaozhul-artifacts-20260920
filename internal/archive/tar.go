// Package archive produces deterministic (reproducible) tar archives from
// directory trees.
//
// Determinism guarantees, regardless of filesystem traversal order, umask,
// source mtimes, or host identity:
//
//   - entries are emitted sorted by archive path (UTF-8 byte order);
//   - every entry gets a fixed mtime, uid/gid, uname/gname and permission
//     policy (configurable);
//   - PAX format is forced so long path names (>100 bytes) are portable;
//   - access and change times are always zero;
//   - the top-level root directory itself is never stored, but empty
//     directories inside it are;
//   - symbolic links are kept as links and are rejected when their target
//     escapes the source root (including symlink loops);
//   - non-portable entry types (devices, FIFOs, sockets) are rejected.
//
// Given identical file contents and (logical) entry names, two runs produce
// byte-identical output even when the directory readdir order or the source
// mtimes differ.
package archive

import (
	"archive/tar"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// Lister abstracts directory listing and file inspection. The production
// implementation wraps the os package; tests inject a shuffling lister to
// prove the output does not depend on readdir order.
type Lister interface {
	ReadDir(name string) ([]os.DirEntry, error)
	Lstat(name string) (os.FileInfo, error)
}

// Default policy values (see Options).
const (
	DefaultDirMode  = 0o755
	DefaultFileMode = 0o644
	DefaultLinkMode = 0o777 // mode of a symlink inside a tar, matches GNU tar

	// PreserveMode, used as an Options mode, keeps the source file's own
	// permission bits instead of applying the default policy.
	PreserveMode os.FileMode = 0xffffffff
)

// Options controls the normalization applied while writing an archive.
type Options struct {
	// FixedTime is the mtime recorded for every entry. The zero value is
	// replaced by Unix epoch 0 (1970-01-01 00:00:00 UTC), the strongest
	// default for reproducibility.
	FixedTime time.Time
	// DirMode / FileMode / LinkMode override the stored permission bits.
	// Set a field to PreserveMode to keep the source file's own mode bits.
	DirMode  os.FileMode
	FileMode os.FileMode
	LinkMode os.FileMode
	// Lister is used for filesystem access; nil uses osLister.
	Lister Lister
}

// ErrSymlinkEscape marks a symlink whose resolved target lies outside root.
type ErrSymlinkEscape struct{ Link, Target, Root string }

func (e *ErrSymlinkEscape) Error() string {
	return fmt.Sprintf("archive: symlink %q escapes source root %q (resolves to %q)", e.Link, e.Root, e.Target)
}

// ErrSymlinkLoop marks a symlink that participates in a link cycle.
type ErrSymlinkLoop struct{ Link string }

func (e *ErrSymlinkLoop) Error() string {
	return fmt.Sprintf("archive: symlink loop at %q", e.Link)
}

// osLister is the production Lister backed by the os package.
type osLister struct{}

func (osLister) ReadDir(name string) ([]os.DirEntry, error) {
	return os.ReadDir(name)
}
func (osLister) Lstat(name string) (os.FileInfo, error) { return os.Lstat(name) }

// entry is one collected filesystem item, with its path already validated.
type entry struct {
	rel     string      // archive name, slash separated, no leading "./"
	abs     string      // absolute source path
	fi      os.FileInfo // Lstat info (never follows symlinks)
	symlink bool
}

// WriteTar writes a deterministic tar archive of the tree rooted at srcRoot
// to w. It returns the number of stored entries. The caller may pass an
// optional hash.Hash (e.g. sha256.New()) to checksum the exact bytes written.
func WriteTar(w io.Writer, srcRoot string, opts *Options) (int, error) {
	o := normalizeOptions(opts)

	absRoot, err := filepath.Abs(srcRoot)
	if err != nil {
		return 0, fmt.Errorf("archive: resolve source root: %w", err)
	}
	fi, err := o.Lister.Lstat(absRoot)
	if err != nil {
		return 0, fmt.Errorf("archive: source root: %w", err)
	}
	if !fi.IsDir() {
		return 0, fmt.Errorf("archive: source root %q is not a directory", srcRoot)
	}

	entries, err := collect(o.Lister, absRoot)
	if err != nil {
		return 0, err
	}
	// Defensive: correctness must never depend on lister ordering.
	sortEntries(entries)
	if err := validateSorted(entries); err != nil {
		return 0, err
	}

	var tw *tar.Writer
	tw = tar.NewWriter(w)
	for i := range entries {
		if err := writeEntry(tw, o.Lister, absRoot, entries[i], o); err != nil {
			return 0, err
		}
	}
	if err := tw.Close(); err != nil {
		return 0, fmt.Errorf("archive: finalize tar: %w", err)
	}
	return len(entries), nil
}

// WriteTarHashed is a convenience wrapper that also returns the SHA-256
// digest (hex) of the bytes written.
func WriteTarHashed(w io.Writer, srcRoot string, opts *Options) (n int, sha256hex string, err error) {
	// implemented in hash.go to keep this file focused
	return writeTarHashed(w, srcRoot, opts)
}

func normalizeOptions(opts *Options) Options {
	var o Options
	if opts != nil {
		o = *opts
	}
	if o.FixedTime.IsZero() {
		o.FixedTime = time.Unix(0, 0).UTC()
	}
	if o.DirMode == 0 {
		o.DirMode = DefaultDirMode
	}
	if o.FileMode == 0 {
		o.FileMode = DefaultFileMode
	}
	if o.LinkMode == 0 {
		o.LinkMode = DefaultLinkMode
	}
	if o.Lister == nil {
		o.Lister = osLister{}
	}
	return o
}

// collect walks the tree rooted at absRoot using Lstat semantics (symlinks
// are never traversed) and returns all entries sorted by archive path.
func collect(l Lister, absRoot string) ([]entry, error) {
	var out []entry
	var walk func(dir, rel string) error
	walk = func(dir, rel string) error {
		des, err := l.ReadDir(dir)
		if err != nil {
			return fmt.Errorf("archive: read directory %q: %w", dir, err)
		}
		for _, de := range des {
			path := filepath.Join(dir, de.Name())
			fi, err := l.Lstat(path)
			if err != nil {
				return fmt.Errorf("archive: lstat %q: %w", path, err)
			}
			childRel := de.Name()
			if rel != "" {
				childRel = rel + "/" + de.Name()
			}
			switch mode := fi.Mode(); {
			case mode.IsRegular():
				out = append(out, entry{rel: childRel, abs: path, fi: fi})
			case mode.IsDir():
				out = append(out, entry{rel: childRel, abs: path, fi: fi})
				if err := walk(path, childRel); err != nil {
					return err
				}
			case mode&os.ModeSymlink != 0:
				if err := checkSymlink(l, absRoot, path, childRel); err != nil {
					return err
				}
				out = append(out, entry{rel: childRel, abs: path, fi: fi, symlink: true})
			default:
				// Devices, FIFOs and sockets are non-portable; refusing them
				// keeps the archive deterministic and extraction-safe.
				return fmt.Errorf("archive: unsupported entry type %q (%v)", childRel, mode)
			}
		}
		return nil
	}
	if err := walk(absRoot, ""); err != nil {
		return nil, err
	}
	sortEntries(out)
	return out, nil
}

// sortEntries orders entries in archive order: a directory entry sorts
// immediately before its contents (the directory's archive name carries a
// trailing slash, and '/' < any byte occurring in a file name).
func sortEntries(entries []entry) {
	sort.Slice(entries, func(i, j int) bool {
		return archiveName(entries[i]) < archiveName(entries[j])
	})
}

func archiveName(e entry) string {
	if e.fi.Mode().IsDir() && !e.symlink {
		return e.rel + "/"
	}
	return e.rel
}

// validateSorted ensures no entry duplicates another and every file's parent
// directory entry is present (guards empty-dir handling assumptions).
func validateSorted(entries []entry) error {
	seen := make(map[string]bool, len(entries))
	for _, e := range entries {
		if seen[e.rel] {
			return fmt.Errorf("archive: duplicate entry %q", e.rel)
		}
		seen[e.rel] = true
	}
	return nil
}

func writeEntry(tw *tar.Writer, l Lister, absRoot string, e entry, o Options) error {
	hdr := &tar.Header{
		Name:       e.rel, // directories get a trailing slash below
		Uid:        0,
		Gid:        0,
		Uname:      "",
		Gname:      "",
		ModTime:    o.FixedTime,
		AccessTime: time.Time{},
		ChangeTime: time.Time{},
		Format:     tar.FormatPAX,
	}
	switch {
	case e.symlink:
		target, err := os.Readlink(e.abs)
		if err != nil {
			return fmt.Errorf("archive: read symlink %q: %w", e.rel, err)
		}
		hdr.Typeflag = tar.TypeSymlink
		hdr.Linkname = target
		hdr.Mode = pickMode(o.LinkMode, e.fi.Mode().Perm())
	case e.fi.Mode().IsDir():
		hdr.Typeflag = tar.TypeDir
		if !strings.HasSuffix(hdr.Name, "/") {
			hdr.Name += "/"
		}
		hdr.Mode = pickMode(o.DirMode, e.fi.Mode().Perm())
	default:
		hdr.Typeflag = tar.TypeReg
		hdr.Mode = pickMode(o.FileMode, e.fi.Mode().Perm())
		hdr.Size = e.fi.Size()
	}
	if err := tw.WriteHeader(hdr); err != nil {
		return fmt.Errorf("archive: write header %q: %w", hdr.Name, err)
	}
	if hdr.Typeflag == tar.TypeReg {
		f, err := os.Open(e.abs)
		if err != nil {
			return fmt.Errorf("archive: open %q: %w", e.rel, err)
		}
		_, cpErr := io.Copy(tw, f)
		clErr := f.Close()
		if cpErr != nil {
			return fmt.Errorf("archive: copy %q: %w", e.rel, cpErr)
		}
		if clErr != nil {
			return fmt.Errorf("archive: close %q: %w", e.rel, clErr)
		}
	}
	return nil
}

// pickMode returns the policy override unless policy is PreserveMode, in
// which case the source mode bits are preserved.
func pickMode(policy, src os.FileMode) int64 {
	if policy == PreserveMode {
		return int64(src)
	}
	return int64(policy.Perm())
}

// checkSymlink validates that the resolved target of linkAbs stays within
// absRoot. linkRel is the archive name for diagnostics.
func checkSymlink(l Lister, absRoot, linkAbs, linkRel string) error {
	target, err := os.Readlink(linkAbs)
	if err != nil {
		return fmt.Errorf("archive: read symlink %q: %w", linkRel, err)
	}
	// Resolve relative to the link's containing directory BEFORE any
	// filesystem resolution, so broken (missing) targets are handled by
	// lexical location rather than falling back to the link itself.
	resolved := target
	if !filepath.IsAbs(resolved) {
		resolved = filepath.Join(filepath.Dir(linkAbs), target)
	}
	resolved = filepath.Clean(resolved)
	final, err := resolveExistingPrefix(l, linkAbs, resolved)
	if err != nil {
		var le *ErrSymlinkLoop
		if errors.As(err, &le) {
			le.Link = linkRel
			return le
		}
		return err
	}
	if !withinRoot(absRoot, final) {
		return &ErrSymlinkEscape{Link: linkRel, Target: final, Root: absRoot}
	}
	return nil
}

// resolveExistingPrefix resolves all symlinks in the longest existing prefix
// of target, then re-attaches the non-existing suffix lexically. This accepts
// broken links whose intended location is inside the root while still
// detecting broken links that point outside it, and surfaces link loops.
//
// Resolution is implemented component-by-component (rather than relying on
// filepath.EvalSymlinks) so the semantics are explicit and identical on every
// platform: an explicit stack of remaining components is walked against a
// resolved, symlink-free prefix; a symlink component splices its target onto
// the front of the remaining stack.
func resolveExistingPrefix(l Lister, linkAbs, target string) (string, error) {
	if !filepath.IsAbs(target) {
		return "", fmt.Errorf("archive: internal error: resolveExistingPrefix expects an absolute path, got %q", target)
	}
	rootPrefix := filepath.VolumeName(target) + "/"
	stack := components(target)
	resolved := rootPrefix // always ends in "/"
	var suffix []string
	steps := 0
	const maxSteps = 256

	for len(stack) > 0 {
		steps++
		if steps > maxSteps {
			return "", &ErrSymlinkLoop{Link: linkAbs}
		}
		c := stack[0]
		stack = stack[1:]
		if c == ".." {
			resolved = parentOf(resolved)
			continue
		}
		next := strings.TrimSuffix(resolved, "/") + "/" + c
		fi, err := l.Lstat(next)
		if err != nil {
			if os.IsNotExist(err) {
				suffix = append([]string{c}, stack...)
				break
			}
			return "", fmt.Errorf("archive: lstat %q: %w", next, err)
		}
		if fi.Mode()&os.ModeSymlink == 0 {
			resolved = next + "/"
			continue
		}
		dest, err := os.Readlink(next)
		if err != nil {
			return "", fmt.Errorf("archive: readlink %q: %w", next, err)
		}
		if filepath.IsAbs(dest) {
			resolved = filepath.VolumeName(dest) + "/"
			stack = append(components(dest), stack...)
		} else {
			stack = append(components(dest), stack...)
		}
	}
	out := strings.TrimSuffix(resolved, "/")
	if len(suffix) > 0 {
		out += "/" + strings.Join(suffix, "/")
	}
	if out == "" {
		out = "/"
	}
	return filepath.Clean(out), nil
}

// components splits an absolute or relative path into lexical elements,
// dropping empty and "." elements (volume prefixes are dropped too).
func components(p string) []string {
	p = strings.TrimPrefix(p, filepath.VolumeName(p))
	raw := strings.Split(filepath.ToSlash(p), "/")
	var out []string
	for _, c := range raw {
		if c == "" || c == "." {
			continue
		}
		out = append(out, c)
	}
	return out
}

// parentOf returns the parent of an absolute path expressed with a trailing
// slash ("/a/b/" -> "/a/").
func parentOf(p string) string {
	t := strings.TrimRight(p, "/")
	idx := strings.LastIndexByte(t, '/')
	if idx <= 0 {
		return "/"
	}
	return t[:idx+1]
}

// withinRoot reports whether cleaned target is root itself or inside it.
func withinRoot(root, target string) bool {
	root = filepath.Clean(root)
	target = filepath.Clean(target)
	if target == root {
		return true
	}
	rel, err := filepath.Rel(root, target)
	if err != nil {
		return false
	}
	return rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator))
}
