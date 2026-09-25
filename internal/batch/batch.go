// Package batch validates a batch of unified-diff requests against a work
// directory and, only if every patch validates, publishes them atomically:
// each change is staged first, installed with rename, and backed up so that a
// failure mid-publish rolls the work directory back to its original state.
package batch

import (
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync/atomic"

	"patchd/internal/apply"
	"patchd/internal/diff"
	"patchd/internal/paths"
)

// Request is one patch in a batch.
type Request struct {
	Diff     string `json:"diff"`
	Encoding string `json:"encoding,omitempty"` // "" or "utf-8"; "base64" is also accepted
}

// FileResult reports the outcome for one file.
type FileResult struct {
	Path      string      `json:"path"`
	Operation string      `json:"operation"` // create | modify | delete | noop
	Stats     apply.Stats `json:"stats"`
}

// Failure is a single validation or publish error.
type Failure struct {
	Index   int    `json:"index"`
	Path    string `json:"path,omitempty"`
	Code    string `json:"code"`
	Message string `json:"message"`
	Hunk    int    `json:"hunk,omitempty"`
	Line    int    `json:"line,omitempty"`
}

func (f *Failure) Error() string {
	if f.Path != "" {
		return fmt.Sprintf("patch %d (%s): %s: %s", f.Index, f.Path, f.Code, f.Message)
	}
	return fmt.Sprintf("patch %d: %s: %s", f.Index, f.Code, f.Message)
}

type opKind int

const (
	opCreate opKind = iota
	opModify
	opDelete
	opNoop
)

type action struct {
	target     string // absolute workdir path
	rel        string // workdir-relative path
	kind       opKind
	blob       string // staged blob path in the cache dir (create/modify)
	blobMode   os.FileMode
	result     FileResult
	backup     string // hidden backup file inside the target directory
	placed     bool   // file installed at target during commit
	selfUndone bool   // install() failed after restoring the target itself
	created    bool   // target did not exist before commit (for create rollback)
}

// Plan is a fully validated, staged batch ready to commit.
type Plan struct {
	root    string
	actions []*action
	Results []FileResult
	mkdirs  []string // directories created during the current commit
}

var tmpCounter uint64

func tmpName(prefix string) string {
	n := atomic.AddUint64(&tmpCounter, 1)
	return fmt.Sprintf("%s-%d-%d", prefix, os.Getpid(), n)
}

// PlanBatch validates every request and stages resulting blobs in cacheDir.
// On any failure it returns a *Failure and leaves the work directory
// untouched (staged cache files are removed).
func PlanBatch(root, cacheDir string, reqs []Request) (*Plan, error) {
	absRoot, err := filepath.Abs(root)
	if err != nil {
		return nil, &Failure{Code: "invalid_root", Message: err.Error()}
	}
	switch fi, err := os.Stat(absRoot); {
	case err != nil:
		return nil, &Failure{Code: "root_not_found", Message: err.Error()}
	case !fi.IsDir():
		return nil, &Failure{Code: "invalid_root", Message: "work directory is not a directory"}
	}
	if len(reqs) == 0 {
		return nil, &Failure{Code: "empty_batch", Message: "no patches supplied"}
	}
	if err := os.MkdirAll(cacheDir, 0o700); err != nil {
		return nil, &Failure{Code: "cache_unavailable", Message: err.Error()}
	}

	plan := &Plan{root: absRoot}
	seen := map[string]int{} // relative target -> request index
	var staged []string
	cleanup := func() {
		for _, s := range staged {
			_ = os.Remove(s)
		}
	}

	for idx, req := range reqs {
		text, err := decode(req)
		if err != nil {
			cleanup()
			return nil, &Failure{Index: idx, Code: "bad_encoding", Message: err.Error()}
		}
		patches, err := diff.Parse(text)
		if err != nil {
			cleanup()
			return nil, parseFailure(idx, err)
		}
		for _, fp := range patches {
			rel := filepath.ToSlash(fp.Target())
			target, err := paths.Resolve(absRoot, rel)
			if err != nil {
				cleanup()
				return nil, &Failure{Index: idx, Path: rel, Code: paths.CodeUnsafePath, Message: err.Error()}
			}
			if prev, dup := seen[rel]; dup {
				cleanup()
				return nil, &Failure{Index: idx, Path: rel, Code: "duplicate_target",
					Message: fmt.Sprintf("file is also modified by patch %d", prev)}
			}
			seen[rel] = idx

			original, existed, mode, err := readTarget(target, rel)
			if err != nil {
				cleanup()
				return nil, &Failure{Index: idx, Path: rel, Code: "target_not_regular", Message: err.Error()}
			}

			result, st, verr := apply.Verify(fp, original)
			if verr != nil {
				cleanup()
				return nil, applyFailure(idx, rel, verr)
			}

			a := &action{target: target, rel: rel, blobMode: mode}
			fr := FileResult{Path: rel, Stats: st}
			switch {
			case !st.WillExist:
				a.kind = opDelete
				fr.Operation = "delete"
			case !existed:
				a.kind = opCreate
				fr.Operation = "create"
				if a.blobMode == 0 {
					a.blobMode = 0o644
				}
			case !st.Changed:
				a.kind = opNoop
				fr.Operation = "noop"
			default:
				a.kind = opModify
				fr.Operation = "modify"
			}

			if a.kind == opCreate || a.kind == opModify {
				blob := filepath.Join(cacheDir, tmpName("blob"))
				if err := os.WriteFile(blob, result, 0o600); err != nil {
					cleanup()
					return nil, &Failure{Index: idx, Path: rel, Code: "stage_failed", Message: err.Error()}
				}
				staged = append(staged, blob)
				a.blob = blob
			}
			a.result = fr
			plan.actions = append(plan.actions, a)
			plan.Results = append(plan.Results, fr)
		}
	}
	// Deterministic publish order (by path) regardless of request order.
	sort.SliceStable(plan.actions, func(i, j int) bool { return plan.actions[i].rel < plan.actions[j].rel })
	return plan, nil
}

// Commit publishes every action. On failure it rolls completed actions back
// and returns a *Failure; a returned failure after rollback means rollback
// itself could not fully restore the work directory.
func (p *Plan) Commit() (err error) {
	done := 0
	p.mkdirs = nil
	defer func() {
		if err == nil {
			p.removeBlobs()
		}
	}()

	for _, a := range p.actions {
		if a.kind == opNoop {
			continue
		}
		if err := p.install(a); err != nil {
			p.rollback(p.actions[:done])
			return &Failure{Code: "publish_failed", Path: a.rel, Message: err.Error()}
		}
		done++
	}
	// Every action is in place. Backups for modify/delete are no longer
	// needed; remove them so the tree matches the intended final state.
	for _, a := range p.actions {
		if a.backup != "" {
			_ = os.Remove(a.backup)
		}
	}
	return nil
}

// ensureMkdir creates the target's parent directory, recording it so rollback
// can remove directories this batch introduced.
func (p *Plan) ensureMkdir(target string) error {
	dir := filepath.Dir(target)
	if _, err := os.Stat(dir); err == nil {
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	// Record which components are missing beforehand, so rollback removes
	// exactly the directories this batch introduces.
	rel, err := filepath.Rel(p.root, dir)
	if err != nil {
		return err
	}
	var missing []string
	cur := p.root
	for _, part := range strings.Split(filepath.ToSlash(rel), "/") {
		if part == "." || part == "" {
			continue
		}
		cur = filepath.Join(cur, part)
		if _, err := os.Stat(cur); err != nil && errors.Is(err, os.ErrNotExist) {
			missing = append(missing, cur)
		}
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	p.mkdirs = append(p.mkdirs, missing...)
	return nil
}

func (p *Plan) install(a *action) error {
	// Re-validate the path at publish time: a symlink introduced after
	// planning must not redirect the write outside the work directory.
	if _, err := paths.Resolve(p.root, a.rel); err != nil {
		return err
	}
	if err := p.ensureMkdir(a.target); err != nil {
		return err
	}
	switch a.kind {
	case opDelete:
		fi, err := os.Lstat(a.target)
		if err != nil {
			return err
		}
		if fi.Mode()&os.ModeSymlink != 0 || !fi.Mode().IsRegular() {
			return fmt.Errorf("refusing to delete non-regular file")
		}
		a.backup = filepath.Join(filepath.Dir(a.target), tmpName(".patchd-backup"))
		if err := os.Rename(a.target, a.backup); err != nil {
			return err
		}
		a.placed = true
		a.created = false
		return nil
	case opCreate:
		if _, err := os.Lstat(a.target); err == nil {
			return fmt.Errorf("target appeared between validation and publish")
		} else if !errors.Is(err, os.ErrNotExist) {
			return err
		}
		tmp := filepath.Join(filepath.Dir(a.target), tmpName(".patchd-tmp"))
		if err := copyFile(a.blob, tmp, a.blobMode); err != nil {
			return err
		}
		if err := os.Rename(tmp, a.target); err != nil {
			_ = os.Remove(tmp)
			return err
		}
		a.placed = true
		a.created = true
		return nil
	case opModify:
		fi, err := os.Lstat(a.target)
		if err != nil {
			return err
		}
		if fi.Mode()&os.ModeSymlink != 0 || !fi.Mode().IsRegular() {
			return fmt.Errorf("target is no longer a regular file")
		}
		a.backup = filepath.Join(filepath.Dir(a.target), tmpName(".patchd-backup"))
		if err := os.Rename(a.target, a.backup); err != nil {
			return err
		}
		a.created = false
		tmp := filepath.Join(filepath.Dir(a.target), tmpName(".patchd-tmp"))
		if err := copyFile(a.blob, tmp, a.blobMode); err != nil {
			// Restore immediately; the rename pair is atomic within one dir.
			_ = os.Rename(a.backup, a.target)
			_ = os.Remove(a.backup)
			a.selfUndone = true
			return err
		}
		if err := os.Rename(tmp, a.target); err != nil {
			_ = os.Remove(tmp)
			_ = os.Rename(a.backup, a.target)
			_ = os.Remove(a.backup)
			a.selfUndone = true
			return err
		}
		a.placed = true
		return nil
	}
	return nil
}

func (p *Plan) rollback(actions []*action) {
	for i := len(actions) - 1; i >= 0; i-- {
		a := actions[i]
		if !a.placed || a.selfUndone {
			continue
		}
		switch a.kind {
		case opCreate:
			_ = os.Remove(a.target)
		case opModify:
			_ = os.Remove(a.target)
			_ = os.Rename(a.backup, a.target)
		case opDelete:
			_ = os.Rename(a.backup, a.target)
		}
	}
	// Remove directories introduced by this batch, deepest first. Skip any
	// that are non-empty (e.g. another, unrelated file appeared).
	for i := len(p.mkdirs) - 1; i >= 0; i-- {
		_ = os.Remove(p.mkdirs[i])
	}
	p.removeBlobs()
}

func (p *Plan) removeBlobs() {
	for _, a := range p.actions {
		if a.blob != "" {
			_ = os.Remove(a.blob)
		}
	}
}

func readTarget(target, rel string) (content []byte, existed bool, mode os.FileMode, err error) {
	fi, lerr := os.Lstat(target)
	if lerr != nil {
		if errors.Is(lerr, os.ErrNotExist) {
			return nil, false, 0, nil
		}
		return nil, false, 0, lerr
	}
	if fi.Mode()&os.ModeSymlink != 0 {
		return nil, false, 0, fmt.Errorf("target %q is a symlink; replace it outside the patch service", rel)
	}
	if !fi.Mode().IsRegular() {
		return nil, false, 0, fmt.Errorf("target %q is not a regular file", rel)
	}
	content, rerr := os.ReadFile(target)
	if rerr != nil {
		return nil, false, 0, rerr
	}
	return content, true, fi.Mode().Perm(), nil
}

func copyFile(src, dst string, mode os.FileMode) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		_ = os.Remove(dst)
		return err
	}
	if err := out.Close(); err != nil {
		_ = os.Remove(dst)
		return err
	}
	return nil
}

func decode(req Request) ([]byte, error) {
	switch req.Encoding {
	case "", "utf-8", "text":
		if req.Diff == "" {
			return nil, fmt.Errorf("empty diff text")
		}
		return []byte(req.Diff), nil
	case "base64":
		return base64.StdEncoding.DecodeString(req.Diff)
	default:
		return nil, fmt.Errorf("unsupported encoding %q", req.Encoding)
	}
}

func parseFailure(idx int, err error) *Failure {
	f := &Failure{Index: idx, Code: "malformed_patch", Message: err.Error()}
	var de *diff.Error
	if errors.As(err, &de) {
		f.Code = de.Code
		f.Line = de.Line
		if de.File != "" {
			f.Path = de.File
		}
	}
	return f
}

func applyFailure(idx int, rel string, err error) *Failure {
	f := &Failure{Index: idx, Path: rel, Code: "application_failed", Message: err.Error()}
	var ae *apply.Error
	if errors.As(err, &ae) {
		f.Code = ae.Code
		f.Hunk = ae.Hunk
		f.Line = ae.Line
	}
	return f
}
