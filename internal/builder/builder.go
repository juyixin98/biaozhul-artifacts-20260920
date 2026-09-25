// Package builder executes declarative build specs and produces provenance
// records.
//
// Safety model:
//   - Only tools declared in the project's build.json are ever executed, and
//     their resolved paths must live inside that project's directory.
//   - Every action runs in a fresh, disposable working directory (created
//     under the store's work tree, never in the cache). Declared sources are
//     copied to work/<run>-<action>/src/<path>; upstream artifacts are
//     materialized from the immutable cache to work/.../in/<local>/<path>.
//   - The process environment is stripped to a fixed allowlist, so builds
//     cannot read credentials or vary with the caller's environment.
//   - After execution, every declared output is hashed from disk and moved
//     into the content-addressed cache. Outputs must stay inside the work dir.
//   - An input fingerprint (tool, args, sources, upstream digests) acts as
//     the cache key: identical inputs reuse the previously recorded result
//     without executing the tool again.
package builder

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"time"

	"bis/internal/digest"
	"bis/internal/provenance"
	"bis/internal/safeio"
	"bis/internal/spec"
	"bis/internal/store"
)

// Builder executes projects against a store.
type Builder struct {
	st      *store.Store
	timeout time.Duration
	now     func() time.Time
}

// Option configures a Builder.
type Option func(*Builder)

// WithTimeout sets the per-action execution timeout (0 = default 30s).
func WithTimeout(d time.Duration) Option {
	return func(b *Builder) { b.timeout = d }
}

// New creates a Builder.
func New(st *store.Store, opts ...Option) *Builder {
	b := &Builder{st: st, timeout: 30 * time.Second, now: time.Now}
	for _, o := range opts {
		o(b)
	}
	return b
}

// ExecutedAction is the outcome for one action in a build report.
type ExecutedAction struct {
	ActionID         string         `json:"action_id"`
	Status           string         `json:"status"` // "executed" | "cached" | "failed"
	RecordID         digest.Digest  `json:"record_id,omitempty"`
	InputFingerprint digest.Digest  `json:"input_fingerprint,omitempty"`
	Outputs          []OutputResult `json:"outputs,omitempty"`
	Command          string         `json:"command,omitempty"`
	Error            string         `json:"error,omitempty"`
}

// OutputResult describes one cached output.
type OutputResult struct {
	Path   string        `json:"path"`
	Digest digest.Digest `json:"digest"`
	Bytes  int64         `json:"bytes"`
	Cached bool          `json:"cached"`
}

// Report is the result of a build run.
type Report struct {
	Project     string           `json:"project"`
	StartedAt   string           `json:"started_at"`
	FinishedAt  string           `json:"finished_at"`
	Status      string           `json:"status"` // "ok" | "failed"
	Actions     []ExecutedAction `json:"actions"`
	CommandsRun []string         `json:"commands_run"` // fixture commands actually spawned
}

// Build validates and executes the whole project. On action failure the
// remaining actions are skipped and the report (with Status "failed") is
// still returned alongside the error.
func (b *Builder) Build(p *spec.Project) (*Report, error) {
	order, err := p.TopoOrder()
	if err != nil {
		return nil, err
	}
	runTag := b.now().UTC().Format("20060102T150405.000000000")
	rep := &Report{Project: p.Name, StartedAt: b.now().UTC().Format(time.RFC3339Nano)}
	idx := b.st.LoadIndex(p.Name)
	if idx.Records == nil {
		idx.Records = map[string]store.RecordEntry{}
	}

	var buildErr error
	failed := map[string]bool{}
	for _, i := range order {
		a := &p.Actions[i]
		skip := false
		for _, up := range a.Upstream {
			if failed[up] {
				skip = true
			}
		}
		switch {
		case skip:
			rep.Actions = append(rep.Actions, ExecutedAction{
				ActionID: a.ID, Status: "failed",
				Error: "upstream action failed; skipped",
			})
			failed[a.ID] = true
		default:
			ea, err := b.runAction(p, a, idx, runTag)
			rep.Actions = append(rep.Actions, ea)
			if err != nil {
				buildErr = errors.Join(buildErr, fmt.Errorf("action %q: %w", a.ID, err))
				failed[a.ID] = true
			}
			if ea.Command != "" {
				rep.CommandsRun = append(rep.CommandsRun, ea.Command)
			}
		}
	}

	if err := b.st.SaveIndex(idx); err != nil {
		return rep, err
	}
	rep.FinishedAt = b.now().UTC().Format(time.RFC3339Nano)
	if buildErr != nil {
		rep.Status = "failed"
		return rep, buildErr
	}
	rep.Status = "ok"
	return rep, nil
}

func (b *Builder) runAction(p *spec.Project, a *spec.Action, idx *store.Index, runTag string) (ExecutedAction, error) {
	ea := ExecutedAction{ActionID: a.ID}

	// --- Resolve and hash the tool (must live inside the project tree). ---
	toolPath, err := b.st.ResolveProjectPath(p.Name, a.Tool)
	if err != nil {
		ea.Status, ea.Error = "failed", "tool resolution: "+err.Error()
		return ea, err
	}
	info, err := os.Stat(toolPath)
	if err != nil {
		ea.Status, ea.Error = "failed", "tool stat: "+err.Error()
		return ea, err
	}
	if !info.Mode().IsRegular() {
		err := fmt.Errorf("tool %q is not a regular file", a.Tool)
		ea.Status, ea.Error = "failed", err.Error()
		return ea, err
	}
	toolDigest, err := digest.OfFile(toolPath)
	if err != nil {
		ea.Status, ea.Error = "failed", err.Error()
		return ea, err
	}
	toolRef := provenance.ToolRef{Path: a.Tool, Args: append([]string(nil), a.Args...), Digest: toolDigest}

	// --- Resolve and hash declared sources. ---
	srcs := make([]provenance.SourceRef, 0, len(a.Sources))
	for _, sp := range a.Sources {
		full, err := b.st.ResolveProjectPath(p.Name, sp)
		if err != nil {
			ea.Status, ea.Error = "failed", "source resolution: "+err.Error()
			return ea, err
		}
		d, err := digest.OfFile(full)
		if err != nil {
			ea.Status, ea.Error = "failed", fmt.Sprintf("source %q: %v", sp, err)
			return ea, fmt.Errorf("source %q: %w", sp, err)
		}
		srcs = append(srcs, provenance.SourceRef{Path: sp, Digest: d})
	}
	sort.Slice(srcs, func(i, j int) bool { return srcs[i].Path < srcs[j].Path })

	// --- Resolve upstream artifacts from prior provenance. ---
	// Two local names may point at the same upstream action; its outputs are
	// materialized twice but counted only once in the input fingerprint.
	locals := make(map[string]string, len(a.Upstream))
	upSeen := map[string]bool{} // actionID\0output
	ups := make([]provenance.UpstreamRef, 0, len(a.Upstream))
	localNames := make([]string, 0, len(a.Upstream))
	for local := range a.Upstream {
		localNames = append(localNames, local)
	}
	sort.Strings(localNames)
	for _, local := range localNames {
		upID := a.Upstream[local]
		ent, ok := idx.Records[upID]
		if !ok {
			err := fmt.Errorf("upstream %q has no recorded output", upID)
			ea.Status, ea.Error = "failed", err.Error()
			return ea, err
		}
		rec, err := b.st.GetRecord(ent.RecordID)
		if err != nil {
			ea.Status, ea.Error = "failed", err.Error()
			return ea, err
		}
		for _, o := range rec.Outputs {
			if !b.st.HasBlob(o.Digest) {
				err := fmt.Errorf("upstream %q output %q missing from cache", upID, o.Path)
				ea.Status, ea.Error = "failed", err.Error()
				return ea, err
			}
			key := upID + "\x00" + o.Path
			if !upSeen[key] {
				upSeen[key] = true
				ups = append(ups, provenance.UpstreamRef{ActionID: upID, Output: o.Path, Digest: o.Digest})
			}
		}
		locals[local] = upID
	}
	sort.Slice(ups, func(i, j int) bool {
		if ups[i].ActionID != ups[j].ActionID {
			return ups[i].ActionID < ups[j].ActionID
		}
		return ups[i].Output < ups[j].Output
	})

	fp := provenance.ComputeFingerprint(provenance.FingerprintInput{
		ProjectName: p.Name, ActionID: a.ID, Tool: toolRef, Sources: srcs, Upstreams: ups,
	})
	ea.InputFingerprint = fp

	// --- Cache hit: identical inputs -> reuse the recorded execution. ---
	if rid, ok := b.st.FingerprintLookup(fp); ok {
		if rec, err := b.st.GetRecord(rid); err == nil && rec.ActionID == a.ID {
			ea.Status = "cached"
			ea.RecordID = rid
			for _, o := range rec.Outputs {
				ea.Outputs = append(ea.Outputs, OutputResult{Path: o.Path, Digest: o.Digest, Bytes: o.Bytes, Cached: true})
			}
			idx.Records[a.ID] = store.RecordEntry{RecordID: rid, Outputs: outputMap(rec.Outputs)}
			return ea, nil
		}
	}

	// --- Prepare a fresh disposable working directory. ---
	work, err := b.st.NewWorkDir(runTag, a.ID)
	if err != nil {
		ea.Status, ea.Error = "failed", err.Error()
		return ea, err
	}
	defer b.st.RemoveWorkDir(work)

	srcDir := work // declared source paths are reproduced relative to the work dir
	inDir := filepath.Join(work, "in")
	if len(locals) > 0 {
		if err := os.MkdirAll(inDir, 0o755); err != nil {
			ea.Status, ea.Error = "failed", err.Error()
			return ea, err
		}
	}
	for _, s := range srcs {
		full, err := b.st.ResolveProjectPath(p.Name, s.Path)
		if err != nil {
			ea.Status, ea.Error = "failed", err.Error()
			return ea, err
		}
		dst, err := safeio.ResolveWithin(srcDir, s.Path)
		if err != nil {
			ea.Status, ea.Error = "failed", err.Error()
			return ea, err
		}
		if err := safeio.CopyFile(dst, full, infoMode(full, 0o444)); err != nil {
			ea.Status, ea.Error = "failed", err.Error()
			return ea, err
		}
	}
	for local, upID := range locals {
		ent := idx.Records[upID]
		rec, err := b.st.GetRecord(ent.RecordID)
		if err != nil {
			ea.Status, ea.Error = "failed", err.Error()
			return ea, err
		}
		for _, o := range rec.Outputs {
			dst, err := safeio.ResolveWithin(inDir, filepath.Join(local, o.Path))
			if err != nil {
				ea.Status, ea.Error = "failed", err.Error()
				return ea, err
			}
			if err := safeio.CopyFile(dst, b.st.BlobPath(o.Digest), 0o444); err != nil {
				ea.Status, ea.Error = "failed", err.Error()
				return ea, err
			}
		}
	}

	// --- Execute the declared fixture command. ---
	cmdLine := append([]string{a.Tool}, a.Args...)
	ea.Command = shellJoin(cmdLine)
	ctx, cancel := context.WithTimeout(context.Background(), b.timeout)
	defer cancel()
	if err := os.MkdirAll(filepath.Join(work, "tmp"), 0o755); err != nil {
		ea.Status, ea.Error = "failed", err.Error()
		return ea, err
	}
	cmd := exec.CommandContext(ctx, toolPath, a.Args...)
	cmd.Dir = work
	cmd.Env = fixtureEnv(work)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	cmd.Stdout = nil
	runErr := cmd.Run()
	if runErr != nil {
		err := fmt.Errorf("tool exited with error: %v%s", runErr, tailStderr(stderr.String()))
		ea.Status, ea.Error = "failed", err.Error()
		return ea, err
	}

	// --- Hash and cache every declared output. ---
	outs := make([]provenance.OutputRef, 0, len(a.Outputs))
	for _, op := range a.Outputs {
		full, err := safeio.ResolveWithin(work, op)
		if err != nil {
			ea.Status, ea.Error = "failed", err.Error()
			return ea, err
		}
		st, err := os.Stat(full)
		if err != nil {
			ea.Status, ea.Error = "failed", fmt.Sprintf("output %q missing: %v", op, err)
			return ea, fmt.Errorf("output %q: %w", op, err)
		}
		if !st.Mode().IsRegular() {
			err := fmt.Errorf("output %q is not a regular file", op)
			ea.Status, ea.Error = "failed", err.Error()
			return ea, err
		}
		d, err := b.st.PutBlob(full)
		if err != nil {
			ea.Status, ea.Error = "failed", err.Error()
			return ea, err
		}
		outs = append(outs, provenance.OutputRef{Path: op, Digest: d, Bytes: st.Size()})
		ea.Outputs = append(ea.Outputs, OutputResult{Path: op, Digest: d, Bytes: st.Size(), Cached: false})
	}
	sort.Slice(outs, func(i, j int) bool { return outs[i].Path < outs[j].Path })

	// --- Sign and store the provenance record. ---
	rec := &provenance.Record{
		Schema:           provenance.CanonicalVersion,
		Project:          p.Name,
		ActionID:         a.ID,
		Tool:             toolRef,
		Sources:          srcs,
		Upstreams:        ups,
		Outputs:          outs,
		InputFingerprint: fp,
		CreatedAt:        b.now().UTC().Format(time.RFC3339Nano),
	}
	rid, err := b.st.PutRecord(rec)
	if err != nil {
		ea.Status, ea.Error = "failed", err.Error()
		return ea, err
	}
	if err := b.st.FingerprintStore(fp, rid); err != nil {
		ea.Status, ea.Error = "failed", err.Error()
		return ea, err
	}
	idx.Records[a.ID] = store.RecordEntry{RecordID: rid, Outputs: outputMap(outs)}
	ea.Status = "executed"
	ea.RecordID = rid
	return ea, nil
}

func outputMap(outs []provenance.OutputRef) map[string]digest.Digest {
	m := make(map[string]digest.Digest, len(outs))
	for _, o := range outs {
		m[o.Path] = o.Digest
	}
	return m
}

func infoMode(path string, fallback os.FileMode) os.FileMode {
	if st, err := os.Stat(path); err == nil {
		return st.Mode()
	}
	return fallback
}

func tailStderr(s string) string {
	if s == "" {
		return ""
	}
	const n = 400
	if len(s) > n {
		s = "..." + s[len(s)-n:]
	}
	return "; stderr: " + s
}

func shellJoin(parts []string) string {
	out := ""
	for i, p := range parts {
		if i > 0 {
			out += " "
		}
		if needsQuote(p) {
			out += "'" + p + "'"
		} else {
			out += p
		}
	}
	return out
}

func needsQuote(s string) bool {
	if s == "" {
		return true
	}
	for _, r := range s {
		switch r {
		case ' ', '\t', '\n', '\'', '"', ';', '|', '&', '$', '`', '<', '>', '*', '?', '(', ')', '{', '}', '[', ']', '!', '#', '~':
			return true
		}
	}
	return false
}

// fixtureEnv is the fixed, minimal environment given to executed fixtures.
// No caller environment variable leaks through; PATH is not even set.
func fixtureEnv(work string) []string {
	return []string{
		"PATH=/usr/local/bin:/usr/bin:/bin",
		"LANG=C.UTF-8",
		"LC_ALL=C",
		"TMPDIR=" + filepath.Join(work, "tmp"),
	}
}
