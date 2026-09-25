// Package engine executes content-driven incremental builds over a graph.
//
// Build order is a stable topological sort of the requested target
// closure. For every node it computes a fingerprint (see package
// fingerprint), compares it with per-project history, consults the
// content-addressed cache, and either restores outputs ("cached") or
// runs the node's explicitly declared command ("built"). Failed nodes
// never publish a cache entry; their dependents are skipped ("blocked").
package engine

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"sort"
	"strings"
	"time"

	"cdag/internal/cache"
	"cdag/internal/fingerprint"
	"cdag/internal/graph"
	"cdag/internal/spec"
)

// Status values for NodeResult.
const (
	StatusBuilt    = "built"    // command ran successfully this build
	StatusCached   = "cached"   // outputs restored from cache
	StatusVerified = "verified" // no-command node whose files were verified
	StatusFailed   = "failed"   // command failed or a required file is missing
	StatusBlocked  = "blocked"  // skipped because a dependency failed
)

// Change describes one reason a node's fingerprint differs from last build.
type Change struct {
	// Type is a stable machine-readable code, e.g. "input_changed".
	Type string `json:"type"`
	// Detail is a human-readable English explanation.
	Detail string `json:"detail"`
}

// NodeResult is the outcome of building one node.
type NodeResult struct {
	NodeID     string                `json:"node_id"`
	Status     string                `json:"status"`
	Cached     bool                  `json:"cached"`
	Key        string                `json:"key,omitempty"`
	Reason     string                `json:"reason"`
	Changes    []Change              `json:"changes,omitempty"`
	DurationMS int64                 `json:"duration_ms"`
	Outputs    []fingerprint.FileRef `json:"outputs,omitempty"`
	ExitCode   *int                  `json:"exit_code,omitempty"`
	Stdout     string                `json:"stdout,omitempty"`
	Stderr     string                `json:"stderr,omitempty"`
	Error      string                `json:"error,omitempty"`
}

// Report is the full result of a build request.
type Report struct {
	ProjectID  string       `json:"project_id"`
	Workdir    string       `json:"workdir"`
	CacheDir   string       `json:"cache_dir"`
	Targets    []string     `json:"targets"`
	Success    bool         `json:"success"`
	DurationMS int64        `json:"duration_ms"`
	Counts     Counts       `json:"counts"`
	Results    []NodeResult `json:"results"`
}

// Counts summarizes node outcomes.
type Counts struct {
	Built    int `json:"built"`
	Cached   int `json:"cached"`
	Verified int `json:"verified"`
	Failed   int `json:"failed"`
	Blocked  int `json:"blocked"`
	Total    int `json:"total"`
}

// Engine runs builds.
type Engine struct {
	store   *cache.Cache
	timeout time.Duration
	now     func() time.Time
}

// Option configures an Engine.
type Option func(*Engine)

// WithTimeout sets the per-node command timeout.
func WithTimeout(d time.Duration) Option {
	return func(e *Engine) { e.timeout = d }
}

// New creates an engine backed by the given cache.
func New(store *cache.Cache, opts ...Option) *Engine {
	e := &Engine{store: store, timeout: 2 * time.Minute, now: time.Now}
	for _, o := range opts {
		o(e)
	}
	return e
}

const maxOutputBytes = 8 * 1024

// cappedBuffer keeps at most maxOutputBytes for stdout/stderr reporting.
type cappedBuffer struct {
	buf   bytes.Buffer
	limit int
}

func (c *cappedBuffer) Write(p []byte) (int, error) {
	if c.limit == 0 {
		c.limit = maxOutputBytes
	}
	remaining := c.limit - c.buf.Len()
	if remaining > 0 {
		if len(p) <= remaining {
			c.buf.Write(p)
		} else {
			c.buf.Write(p[:remaining])
		}
	}
	return len(p), nil
}

func (c *cappedBuffer) String() string {
	s := c.buf.String()
	s = strings.TrimRight(s, "\n")
	if len(s) > maxOutputBytes {
		s = s[:maxOutputBytes] + "\n... (truncated)"
	}
	return s
}

// Build validates the graph and builds the target closure. If targets is
// empty, the whole graph is built. A structurally invalid graph (cycle,
// unknown reference, bad path) is returned as an error before any command
// runs; node-level failures are reported in the report.
func (e *Engine) Build(p *spec.Project, targets []string) (*Report, error) {
	start := e.now()
	if err := p.Validate(); err != nil {
		return nil, err
	}
	order, gerr := p.Graph.Topo()
	if gerr != nil {
		return nil, gerr
	}

	closure := map[string]bool{}
	if len(targets) == 0 {
		for _, id := range order {
			closure[id] = true
		}
		targets = append([]string(nil), order...)
	} else {
		cl, gerr := p.Graph.Closure(targets)
		if gerr != nil {
			return nil, gerr
		}
		closure = cl
	}

	// Absolute, clean workdir for stable file resolution.
	absWork, err := filepathAbs(p.Workdir)
	if err != nil {
		return nil, err
	}

	report := &Report{
		ProjectID: p.ID,
		Workdir:   absWork,
		CacheDir:  e.store.Root(),
		Targets:   append([]string(nil), targets...),
		Success:   true,
	}
	completed := map[string]*NodeResult{}

	for _, id := range order {
		if !closure[id] {
			continue
		}
		n := p.Graph.Node(id)
		res := e.buildNode(p.ID, absWork, n, completed)
		report.Results = append(report.Results, *res)
		completed[id] = res
		switch res.Status {
		case StatusFailed, StatusBlocked:
			report.Success = false
		}
	}

	for _, r := range report.Results {
		switch r.Status {
		case StatusBuilt:
			report.Counts.Built++
		case StatusCached:
			report.Counts.Cached++
		case StatusVerified:
			report.Counts.Verified++
		case StatusFailed:
			report.Counts.Failed++
		case StatusBlocked:
			report.Counts.Blocked++
		}
	}
	report.Counts.Total = len(report.Results)
	report.DurationMS = e.now().Sub(start).Milliseconds()
	return report, nil
}

func nodeOK(status string) bool {
	return status == StatusBuilt || status == StatusCached || status == StatusVerified
}

func (e *Engine) buildNode(projectID, workdir string, n *graph.Node, completed map[string]*NodeResult) *NodeResult {
	start := e.now()
	res := &NodeResult{NodeID: n.ID}
	elapsed := func() { res.DurationMS = e.now().Sub(start).Milliseconds() }

	// 1. Dependency gate.
	for _, dep := range unique(n.DependsOn) {
		d := completed[dep]
		if d == nil || !nodeOK(d.Status) {
			why := "was not built"
			if d != nil {
				switch d.Status {
				case StatusFailed:
					why = fmt.Sprintf("failed (%s)", truncate(d.Error, 120))
				case StatusBlocked:
					why = "was blocked by an upstream failure"
				}
			}
			res.Status = StatusBlocked
			res.Reason = fmt.Sprintf("skipped: dependency %q %s", dep, why)
			elapsed()
			return res
		}
	}

	// 2. Probe tool versions.
	tools := make([]fingerprint.ToolVersion, 0, len(n.Tools))
	for _, t := range n.Tools {
		v, err := probeTool(t)
		if err != nil {
			res.Status = StatusFailed
			res.Error = fmt.Sprintf("tool probe failed for %q: %v", t.Name, err)
			res.Reason = "not built: " + res.Error
			elapsed()
			return res
		}
		tools = append(tools, fingerprint.ToolVersion{Name: t.Name, Version: v})
	}

	// 3. Hash declared inputs (missing => fail, no fingerprint recorded).
	inputRefs, err := fingerprint.HashFiles(workdir, n.Inputs)
	if err != nil {
		res.Status = StatusFailed
		res.Error = fmt.Sprintf("missing or unreadable input: %v", err)
		res.Reason = "not built: " + res.Error
		elapsed()
		return res
	}

	// 4. Collect dependency references (their fingerprint + output content).
	depRefs := e.dependencyRefs(n, completed)

	// 5. Compute fingerprint and key.
	envValues := map[string]string{}
	for _, name := range n.Env {
		envValues[name] = os.Getenv(name)
	}
	fp := fingerprint.Input(n, inputRefs, depRefs, tools, envValues)
	key, err := fp.Key()
	if err != nil {
		res.Status = StatusFailed
		res.Error = fmt.Sprintf("compute fingerprint: %v", err)
		res.Reason = "not built: " + res.Error
		elapsed()
		return res
	}
	res.Key = key

	// For no-command (source/bundle) nodes: verify declared outputs exist,
	// hash them for dependents, but never execute or overwrite files.
	if strings.TrimSpace(n.Command) == "" {
		outRefs, err := fingerprint.HashFiles(workdir, n.Outputs)
		if err != nil {
			res.Status = StatusFailed
			res.Error = fmt.Sprintf("source node missing declared output: %v", err)
			res.Reason = "not built: " + res.Error
			elapsed()
			return res
		}
		res.Outputs = outRefs
		res.Status = StatusVerified
		res.Reason = "source/bundle node: inputs verified, no command to run"
		elapsed()
		return res
	}

	// 6. History comparison for explanations.
	rec, recOK, err := e.store.HistoryRecord(projectID, n.ID)
	if err != nil {
		res.Status = StatusFailed
		res.Error = fmt.Sprintf("read history: %v", err)
		elapsed()
		return res
	}

	// 7. Fast path: identical entry in the content store.
	if e.store.Has(key) {
		meta, err := e.store.Restore(key, workdir)
		if err != nil {
			res.Status = StatusFailed
			res.Error = fmt.Sprintf("restore from cache: %v", err)
			res.Reason = "not built: " + res.Error
			elapsed()
			return res
		}
		res.Outputs = append([]fingerprint.FileRef(nil), meta.Outputs...)
		res.Status = StatusCached
		res.Cached = true
		res.Reason = e.hitReason(rec, recOK, key)
		_ = e.store.SetHistoryRecord(projectID, cache.HistoryRecord{
			NodeID: n.ID, LastKey: key, LastStatus: "success",
		})
		elapsed()
		return res
	}

	// 8. Slow path: explain the miss and run the command.
	res.Changes, res.Reason = e.missReason(projectID, rec, recOK, key, fp)
	exitCode, runErr := e.runCommand(workdir, n, envValues, res)
	if runErr != nil {
		res.Status = StatusFailed
		res.ExitCode = &exitCode
		res.Error = runErr.Error()
		if res.Reason != "" {
			res.Reason += "; "
		}
		res.Reason += fmt.Sprintf("command failed: %v", runErr)
		// Failure is remembered so the next build can explain the rerun;
		// no cache entry is published.
		_ = e.store.SetHistoryRecord(projectID, cache.HistoryRecord{
			NodeID: n.ID, LastKey: key, LastStatus: "failed",
		})
		elapsed()
		return res
	}
	res.ExitCode = &exitCode

	// 9. Verify declared outputs and publish.
	outRefs, err := fingerprint.HashFiles(workdir, n.Outputs)
	if err != nil {
		res.Status = StatusFailed
		res.Error = fmt.Sprintf("declared output missing after successful command: %v", err)
		res.Reason += "; " + res.Error
		_ = e.store.SetHistoryRecord(projectID, cache.HistoryRecord{
			NodeID: n.ID, LastKey: key, LastStatus: "failed",
		})
		elapsed()
		return res
	}
	if err := e.store.WriteEntry(key, n.ID, workdir, outRefs, fp); err != nil {
		res.Status = StatusFailed
		res.Error = fmt.Sprintf("publish cache entry: %v", err)
		elapsed()
		return res
	}
	res.Outputs = outRefs
	res.Status = StatusBuilt
	_ = e.store.SetHistoryRecord(projectID, cache.HistoryRecord{
		NodeID: n.ID, LastKey: key, LastStatus: "success",
	})
	elapsed()
	return res
}

func (e *Engine) dependencyRefs(n *graph.Node, completed map[string]*NodeResult) []fingerprint.DepRef {
	refs := make([]fingerprint.DepRef, 0, len(n.DependsOn))
	for _, dep := range unique(n.DependsOn) {
		d := completed[dep]
		refs = append(refs, fingerprint.DepRef{
			ID:          dep,
			Fingerprint: d.Key,
			Outputs:     append([]fingerprint.FileRef(nil), d.Outputs...),
		})
	}
	sort.Slice(refs, func(i, j int) bool { return refs[i].ID < refs[j].ID })
	return refs
}

func (e *Engine) hitReason(rec cache.HistoryRecord, recOK bool, key string) string {
	switch {
	case !recOK:
		return "cache hit: a content-identical entry already existed (shared cache); reused stored outputs"
	case rec.LastKey != key:
		// Cannot happen for identical keys, retained for clarity.
		return "cache hit: content-identical entry reused"
	case rec.LastStatus == "failed":
		return "cache hit: fingerprint unchanged and a valid entry now exists; previous failed attempt not rerun"
	default:
		return "cache hit: inputs, dependency outputs, params, env and tool versions unchanged"
	}
}

func (e *Engine) missReason(projectID string, rec cache.HistoryRecord, recOK bool, key string, fp fingerprint.Fingerprint) ([]Change, string) {
	if !recOK {
		return nil, "cache miss: no previous build recorded for this node; running command"
	}
	if rec.LastKey == key {
		if rec.LastStatus == "failed" {
			return nil, "cache miss: previous attempt with this fingerprint failed; re-running command"
		}
		return nil, "cache miss: fingerprint matches the last build but the entry is absent from the cache store (pruned or external cache); re-running command"
	}
	// Different fingerprint: recover the previous one from its entry to diff.
	changes := []Change{}
	if meta, err := e.store.Lookup(rec.LastKey); err == nil {
		changes = diffFingerprint(meta.Fingerprint, fp)
	}
	if len(changes) == 0 {
		changes = append(changes, Change{Type: "fingerprint_changed",
			Detail: fmt.Sprintf("fingerprint changed (%s -> %s)", shortKey(rec.LastKey), shortKey(key))})
	}
	parts := make([]string, 0, len(changes))
	for _, c := range changes {
		parts = append(parts, c.Detail)
	}
	return changes, "cache miss: " + strings.Join(parts, "; ") + "; running command"
}

func (e *Engine) runCommand(workdir string, n *graph.Node, envValues map[string]string, res *NodeResult) (int, error) {
	ctx, cancel := context.WithTimeout(context.Background(), e.timeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, "sh", "-c", n.Command)
	cmd.Dir = workdir
	cmd.Env = commandEnv(n.Env, envValues)
	var stdout, stderr cappedBuffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	stdin, err := os.OpenFile(os.DevNull, os.O_RDONLY, 0)
	if err == nil {
		cmd.Stdin = stdin
		defer stdin.Close()
	}

	runErr := cmd.Run()
	res.Stdout = stdout.String()
	res.Stderr = stderr.String()
	if runErr == nil {
		return 0, nil
	}
	var exitErr *exec.ExitError
	if errors.As(runErr, &exitErr) {
		code := exitErr.ExitCode()
		stderrTail := strings.TrimSpace(res.Stderr)
		if stderrTail == "" {
			stderrTail = strings.TrimSpace(res.Stdout)
		}
		return code, fmt.Errorf("exit status %d: %s", code, truncate(stderrTail, 300))
	}
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return -1, fmt.Errorf("command timed out after %s", e.timeout)
	}
	return -1, runErr
}

// commandEnv builds the whitelisted environment: PATH is always included
// (required to locate sh and tools), plus only the node's declared names.
func commandEnv(declared []string, values map[string]string) []string {
	env := []string{}
	if p, ok := os.LookupEnv("PATH"); ok {
		env = append(env, "PATH="+p)
	}
	seen := map[string]bool{"PATH": true}
	for _, name := range declared {
		if seen[name] {
			continue
		}
		seen[name] = true
		env = append(env, name+"="+values[name])
	}
	return env
}

func probeTool(t graph.Tool) (string, error) {
	if len(t.Probe) == 0 {
		return "", nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, t.Probe[0], t.Probe[1:]...)
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &out
	if err := cmd.Run(); err != nil {
		return "", err
	}
	line := strings.TrimSpace(out.String())
	if i := strings.IndexByte(line, '\n'); i >= 0 {
		line = strings.TrimSpace(line[:i])
	}
	return line, nil
}

func unique(in []string) []string {
	seen := map[string]bool{}
	out := make([]string, 0, len(in))
	for _, s := range in {
		if !seen[s] {
			seen[s] = true
			out = append(out, s)
		}
	}
	sort.Strings(out)
	return out
}

func shortKey(k string) string {
	if len(k) <= 12 {
		return k
	}
	return k[:12]
}

func truncate(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
