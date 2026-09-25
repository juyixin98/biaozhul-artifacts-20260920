// Package apply applies the unified-diff subset parsed by package diff with
// strict, exact semantics:
//
//   - hunk line numbers and context/removed lines must match the target file
//     byte for byte at exactly the declared position; no fuzzy matching and
//     no searching for a better fit;
//   - hunks touching the same file must not overlap;
//   - "\ No newline at end of file" markers must match the real EOL state;
//   - a batch is fully validated in memory before anything on disk changes,
//     and publishing uses backups plus temp-file rename, with rollback, so a
//     failed request leaves the work directory byte-for-byte unchanged.
package apply

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"patchsvc/internal/diff"
)

// ChangeKind identifies what one Change does to one path.
type ChangeKind string

const (
	Create ChangeKind = "create"
	Modify ChangeKind = "modify"
	Delete ChangeKind = "delete"
)

// Change is one validated, not-yet-published file mutation.
type Change struct {
	Path    string
	Kind    ChangeKind
	Content []byte // target content for Create/Modify; empty for Delete
}

// ValidateRelPath rejects paths that could escape the work root: absolute
// paths, NUL bytes, empty/"./.."/empty path components. Backslashes are
// rejected too: this service speaks POSIX paths and a backslash in a patch is
// a mistake rather than a valid file name character.
func ValidateRelPath(p string) error {
	if p == "" {
		return errors.New("empty path")
	}
	if strings.ContainsRune(p, 0) {
		return fmt.Errorf("path contains NUL byte: %q", p)
	}
	if strings.HasPrefix(p, "/") {
		return fmt.Errorf("absolute paths are not allowed: %q", p)
	}
	if strings.ContainsAny(p, "\\") {
		return fmt.Errorf("backslashes are not allowed in paths: %q", p)
	}
	for _, seg := range strings.Split(p, "/") {
		switch seg {
		case "", ".", "..":
			return fmt.Errorf("path component %q is not allowed in %q", seg, p)
		}
	}
	return nil
}

type fileLines struct {
	lines      []string
	trailingNL bool // the final line is terminated by "\n"
}

func splitContent(b []byte) fileLines {
	if len(b) == 0 {
		return fileLines{}
	}
	s := string(b)
	trailing := strings.HasSuffix(s, "\n")
	if trailing {
		s = s[:len(s)-1]
	}
	return fileLines{lines: strings.Split(s, "\n"), trailingNL: trailing}
}

func (f fileLines) bytes() []byte {
	if len(f.lines) == 0 {
		return nil
	}
	s := strings.Join(f.lines, "\n")
	if f.trailingNL {
		s += "\n"
	}
	return []byte(s)
}

// Plan parses every patch document, validates every file patch against the
// files currently under root, and returns the complete, ordered list of
// changes. It never modifies the file system. A single mismatch aborts the
// whole batch.
func Plan(root string, patchTexts []string) ([]Change, error) {
	if len(patchTexts) == 0 {
		return nil, errors.New("no patches supplied")
	}
	var all []diff.FilePatch
	for pi, text := range patchTexts {
		fps, err := diff.Parse(text)
		if err != nil {
			return nil, fmt.Errorf("patch %d parse error: %w", pi+1, err)
		}
		all = append(all, fps...)
	}

	seen := make(map[string]bool)
	changes := make([]Change, 0, len(all))
	for _, fp := range all {
		target := fp.NewPath
		if fp.IsDelete() {
			target = fp.OldPath
		}
		if err := ValidateRelPath(target); err != nil {
			return nil, err
		}
		if seen[target] {
			return nil, fmt.Errorf("path %q is patched more than once in the same batch; split it into sequential requests", target)
		}
		seen[target] = true

		ch, err := planOne(root, fp)
		if err != nil {
			return nil, err
		}
		changes = append(changes, ch)
	}
	return changes, nil
}

func planOne(root string, fp diff.FilePatch) (Change, error) {
	target := fp.NewPath
	if fp.IsDelete() {
		target = fp.OldPath
	}
	abs := filepath.Join(root, filepath.FromSlash(target))
	if !withinRoot(root, abs) {
		return Change{}, fmt.Errorf("path %q resolves outside the work directory", target)
	}

	data, err := os.ReadFile(abs)
	switch {
	case err == nil:
		if fp.IsNew() {
			return Change{}, fmt.Errorf("%s: cannot create file, it already exists", target)
		}
	case errors.Is(err, os.ErrNotExist):
		if !fp.IsNew() {
			return Change{}, fmt.Errorf("%s: target file does not exist", target)
		}
		data = nil
	default:
		return Change{}, fmt.Errorf("%s: cannot read target: %w", target, err)
	}
	if info, statErr := os.Lstat(abs); statErr == nil && info.Mode()&os.ModeSymlink != 0 {
		return Change{}, fmt.Errorf("%s: refusing to patch through a symlink", target)
	}

	old := splitContent(data)
	result, err := applyHunks(target, old, fp)
	if err != nil {
		return Change{}, err
	}

	ch := Change{Path: target}
	switch {
	case fp.IsNew():
		ch.Kind = Create
	case fp.IsDelete():
		if len(result.lines) != 0 {
			return Change{}, fmt.Errorf("%s: deletion patch does not remove all %d line(s)", target, len(result.lines))
		}
		ch.Kind = Delete
	default:
		ch.Kind = Modify
	}
	ch.Content = result.bytes()
	return ch, nil
}

// applyHunks performs the exact-match transformation of one file.
func applyHunks(target string, old fileLines, fp diff.FilePatch) (fileLines, error) {
	var out []string
	cursor := 0  // next unconsumed index in old.lines
	prevEnd := 0 // exclusive old-file index consumed by previous hunks
	trailing := old.trailingNL

	for hi, h := range fp.Hunks {
		// For a zero-length old range the start counts as "number of lines
		// before the insertion point" (so 0,0 means the very top).
		idx := h.OldStart - 1
		if h.OldCount == 0 {
			idx = h.OldStart
		}
		if idx < 0 || idx > len(old.lines) {
			return fileLines{}, fmt.Errorf("%s: hunk %d old start %d is outside the file (%d line(s))", target, hi+1, h.OldStart, len(old.lines))
		}
		if idx < prevEnd {
			return fileLines{}, fmt.Errorf("%s: hunk %d at old line %d overlaps the previous hunk (which consumes up to line %d)", target, hi+1, h.OldStart, prevEnd)
		}

		oldSideCount := 0
		for _, l := range h.Lines {
			if l.Kind != diff.Add {
				oldSideCount++
			}
		}
		if oldSideCount != h.OldCount {
			return fileLines{}, fmt.Errorf("%s: hunk %d header declares %d old line(s) but contains %d", target, hi+1, h.OldCount, oldSideCount)
		}
		if idx+oldSideCount > len(old.lines) {
			return fileLines{}, fmt.Errorf("%s: hunk %d reaches line %d but the file has only %d line(s)", target, hi+1, idx+oldSideCount, len(old.lines))
		}

		out = append(out, old.lines[cursor:idx]...)

		oi := idx
		for _, l := range h.Lines {
			switch l.Kind {
			case diff.Context, diff.Delete:
				if old.lines[oi] != l.Text {
					return fileLines{}, &ContextMismatchError{
						Path:     target,
						Hunk:     hi + 1,
						Line:     oi + 1,
						Expected: old.lines[oi],
						Found:    l.Text,
					}
				}
				if l.Kind == diff.Context {
					out = append(out, l.Text)
				}
				oi++
			case diff.Add:
				out = append(out, l.Text)
			}
		}

		// If the hunk consumes the final old line, its newline marker must
		// agree with reality, and the result's EOL state comes from the new
		// side marker. Hunks not touching EOF leave the EOL state untouched.
		if oi == len(old.lines) && oldSideCount > 0 {
			if h.OldNoNewline == old.trailingNL {
				want := "with"
				if h.OldNoNewline {
					want = "without"
				}
				return fileLines{}, fmt.Errorf("%s: hunk %d reaches end of file but its no-newline marker (%s trailing newline) does not match the file", target, hi+1, want)
			}
			trailing = !h.NewNoNewline
		} else if fp.IsNew() && len(old.lines) == 0 {
			trailing = !h.NewNoNewline
		}

		cursor = oi
		prevEnd = oi
	}

	out = append(out, old.lines[cursor:]...)
	return fileLines{lines: out, trailingNL: trailing}, nil
}

// ContextMismatchError describes one exact-match failure.
type ContextMismatchError struct {
	Path     string
	Hunk     int
	Line     int
	Expected string
	Found    string
}

func (e *ContextMismatchError) Error() string {
	return fmt.Sprintf("%s: hunk %d context mismatch at old line %d: file has %q, patch expects %q",
		e.Path, e.Hunk, e.Line, e.Expected, e.Found)
}

func withinRoot(root, abs string) bool {
	rel, err := filepath.Rel(root, abs)
	if err != nil {
		return false
	}
	return rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator))
}

// backup is the pre-publish snapshot of one target path, staged inside the
// cache directory rather than the work directory.
type backup struct {
	path     string // absolute target path in the work directory
	existed  bool
	staged   string // absolute path of the staged copy in the cache directory
	mode     os.FileMode
	modified bool // the mutation has been performed (rollback needed)
}

// Publish writes the validated changes under root. Before touching the work
// directory it snapshots every affected existing file into cacheDir; if any
// write fails, completed mutations are rolled back from those snapshots.
// cacheDir must be outside root.
func Publish(root string, changes []Change, cacheDir string) (err error) {
	if len(changes) == 0 {
		return errors.New("nothing to publish")
	}
	if !directoriesSeparate(root, cacheDir) {
		return fmt.Errorf("cache directory %q must be separate from work directory %q", cacheDir, root)
	}
	stage, err := os.MkdirTemp(cacheDir, "publish-*")
	if err != nil {
		return fmt.Errorf("cannot create staging area in cache dir: %w", err)
	}
	defer func() {
		if rmErr := os.RemoveAll(stage); rmErr != nil && err == nil {
			err = fmt.Errorf("cannot clean staging area: %w", rmErr)
		}
	}()

	backups := make([]backup, len(changes))
	for i, ch := range changes {
		if err := ValidateRelPath(ch.Path); err != nil {
			return err
		}
		abs := filepath.Join(root, filepath.FromSlash(ch.Path))
		b := backup{path: abs}
		if info, statErr := os.Lstat(abs); statErr == nil {
			if info.Mode()&os.ModeSymlink != 0 {
				return fmt.Errorf("%s: refusing to replace a symlink", ch.Path)
			}
			data, rerr := os.ReadFile(abs)
			if rerr != nil {
				return fmt.Errorf("%s: cannot snapshot: %w", ch.Path, rerr)
			}
			staged := filepath.Join(stage, fmt.Sprintf("%d.bak", i))
			if werr := os.WriteFile(staged, data, 0o600); werr != nil {
				return fmt.Errorf("%s: cannot stage snapshot: %w", ch.Path, werr)
			}
			b.existed = true
			b.staged = staged
			b.mode = info.Mode().Perm()
		} else if !errors.Is(statErr, os.ErrNotExist) {
			return fmt.Errorf("%s: cannot stat: %w", ch.Path, statErr)
		}
		backups[i] = b
	}

	for i, ch := range changes {
		b := &backups[i]
		switch ch.Kind {
		case Delete:
			if err := os.Remove(b.path); err != nil {
				return rollback(backups, fmt.Errorf("deleting %s: %w", ch.Path, err))
			}
		case Create, Modify:
			mode := os.FileMode(0o644)
			if b.existed {
				mode = b.mode
			}
			if err := writeFileAtomic(b.path, ch.Content, mode); err != nil {
				return rollback(backups, fmt.Errorf("writing %s: %w", ch.Path, err))
			}
		default:
			return fmt.Errorf("%s: unknown change kind %q", ch.Path, ch.Kind)
		}
		b.modified = true
	}
	return nil
}

// Apply is Plan followed by Publish. A validation failure returns before any
// file-system mutation, so the work directory is untouched.
func Apply(root, cacheDir string, patchTexts []string) ([]Change, error) {
	changes, err := Plan(root, patchTexts)
	if err != nil {
		return nil, err
	}
	if err := Publish(root, changes, cacheDir); err != nil {
		return nil, err
	}
	return changes, nil
}

// rollback restores the work directory from the staged snapshots. It is
// best-effort and joins every error encountered.
func rollback(backups []backup, cause error) error {
	errs := []error{cause}
	for i := len(backups) - 1; i >= 0; i-- {
		b := backups[i]
		if !b.modified {
			continue
		}
		if b.existed {
			data, err := os.ReadFile(b.staged)
			if err != nil {
				errs = append(errs, fmt.Errorf("rollback %s: %w", b.path, err))
				continue
			}
			if err := writeFileAtomic(b.path, data, b.mode); err != nil {
				errs = append(errs, fmt.Errorf("rollback %s: %w", b.path, err))
			}
		} else {
			if err := os.Remove(b.path); err != nil && !errors.Is(err, os.ErrNotExist) {
				errs = append(errs, fmt.Errorf("rollback %s: %w", b.path, err))
			}
		}
	}
	return errors.Join(errs...)
}

// writeFileAtomic writes data to a temp file in the destination directory and
// renames it over the target, so observers never see a partially written
// file. Parent directories are created.
func writeFileAtomic(abs string, data []byte, mode os.FileMode) error {
	dir := filepath.Dir(abs)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, ".patchsvc-tmp-*")
	if err != nil {
		return err
	}
	tmpName := f.Name()
	cleanup := true
	defer func() {
		if cleanup {
			_ = os.Remove(tmpName)
		}
	}()
	if _, err := io.Copy(f, bytes.NewReader(data)); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmpName, mode); err != nil {
		return err
	}
	if err := os.Rename(tmpName, abs); err != nil {
		return err
	}
	cleanup = false
	return nil
}

func directoriesSeparate(a, b string) bool {
	absA, err1 := filepath.Abs(a)
	absB, err2 := filepath.Abs(b)
	if err1 != nil || err2 != nil {
		return false
	}
	if absA == absB {
		return false
	}
	// Separate only if each is strictly outside the other: a parent/child
	// pair gives one direction ".." and the other a plain name.
	relAB, err1 := filepath.Rel(absA, absB)
	relBA, err2 := filepath.Rel(absB, absA)
	if err1 != nil || err2 != nil {
		return false
	}
	return isOutside(relAB) && isOutside(relBA)
}

func isOutside(rel string) bool {
	return rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator))
}
