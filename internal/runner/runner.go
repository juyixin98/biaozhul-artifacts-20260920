// Package runner executes the project's pre-approved test fixture
// commands. Only commands explicitly registered in a fixture manifest may
// run; there is no shell and no network access from this package.
//
// The work directory (command working directory, per-run scratch space)
// and the cache directory (persisted results) are always distinct paths
// so cached data never pollutes a working tree.
package runner

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// Command is one whitelisted fixture command.
type Command struct {
	// Name is the alias callers use, e.g. "go-version".
	Name string `json:"name"`
	// Description is human readable.
	Description string `json:"description"`
	// Program is the absolute (or PATH-resolved) executable. It is
	// resolved against PATH at load time and the resolved path is what
	// actually runs, so a changing PATH cannot swap the binary later.
	Program string `json:"program"`
	// Args are fixed arguments; callers cannot inject their own.
	Args []string `json:"args"`
	// WorkDir optionally overrides the command working directory. It must
	// be a path inside the runner base directory; when empty the isolated
	// scratch work directory is used.
	WorkDir string `json:"workdir,omitempty"`
	// TimeoutSeconds bounds the run; 0 means the package default.
	TimeoutSeconds int `json:"timeout_seconds,omitempty"`

	resolved string
}

// Manifest is the fixture manifest file.
type Manifest struct {
	Commands []Command `json:"commands"`
}

// LoadManifest reads, validates and resolves a fixture manifest.
func LoadManifest(path string) (*Manifest, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read fixture manifest: %w", err)
	}
	var m Manifest
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("parse fixture manifest: %w", err)
	}
	names := map[string]bool{}
	for i := range m.Commands {
		c := &m.Commands[i]
		if c.Name == "" {
			return nil, fmt.Errorf("fixture command at index %d has no name", i)
		}
		if names[c.Name] {
			return nil, fmt.Errorf("duplicate fixture command name %q", c.Name)
		}
		names[c.Name] = true
		if c.Program == "" {
			return nil, fmt.Errorf("fixture %q has no program", c.Name)
		}
		resolved, err := exec.LookPath(c.Program)
		if err != nil {
			return nil, fmt.Errorf("fixture %q: resolve program %q: %w", c.Name, c.Program, err)
		}
		c.resolved = resolved
	}
	return &m, nil
}

// Lookup returns a resolved command by alias.
func (m *Manifest) Lookup(name string) (*Command, bool) {
	for i := range m.Commands {
		if m.Commands[i].Name == name {
			return &m.Commands[i], true
		}
	}
	return nil, false
}

// Names lists whitelisted aliases, sorted.
func (m *Manifest) Names() []string {
	out := make([]string, len(m.Commands))
	for i, c := range m.Commands {
		out[i] = c.Name
	}
	sort.Strings(out)
	return out
}

// Result is one command execution outcome.
type Result struct {
	Command    string `json:"command"`
	Cached     bool   `json:"cached"`
	ExitCode   int    `json:"exit_code"`
	Stdout     string `json:"stdout,omitempty"`
	Stderr     string `json:"stderr,omitempty"`
	DurationMS int64  `json:"duration_ms"`
	RanAt      string `json:"ran_at,omitempty"`
	Error      string `json:"error,omitempty"`
}

// Runner runs whitelisted fixture commands with separate work and cache
// directories.
type Runner struct {
	manifest       *Manifest
	baseDir        string
	workDir        string
	cacheDir       string
	defaultTimeout time.Duration
}

// NewRunner creates a runner. baseDir is the project root that custom
// fixture working directories must stay inside; workDir (scratch) and
// cacheDir (persistent results) must be distinct absolute paths.
func NewRunner(m *Manifest, baseDir, workDir, cacheDir string, defaultTimeout time.Duration) (*Runner, error) {
	if !filepath.IsAbs(baseDir) || !filepath.IsAbs(workDir) || !filepath.IsAbs(cacheDir) {
		return nil, errors.New("base dir, work dir and cache dir must all be absolute paths")
	}
	if workDir == cacheDir {
		return nil, errors.New("work dir and cache dir must be different directories")
	}
	if err := os.MkdirAll(workDir, 0o755); err != nil {
		return nil, fmt.Errorf("create work dir: %w", err)
	}
	if err := os.MkdirAll(cacheDir, 0o755); err != nil {
		return nil, fmt.Errorf("create cache dir: %w", err)
	}
	if defaultTimeout <= 0 {
		defaultTimeout = 30 * time.Second
	}
	return &Runner{
		manifest:       m,
		baseDir:        filepath.Clean(baseDir),
		workDir:        filepath.Clean(workDir),
		cacheDir:       filepath.Clean(cacheDir),
		defaultTimeout: defaultTimeout,
	}, nil
}

// BaseDir, WorkDir and CacheDir expose the path layout.
func (r *Runner) BaseDir() string  { return r.baseDir }
func (r *Runner) WorkDir() string  { return r.workDir }
func (r *Runner) CacheDir() string { return r.cacheDir }

// Names lists the whitelisted fixture aliases.
func (r *Runner) Names() []string { return r.manifest.Names() }

// Describe returns the registered commands' metadata for listing.
func (r *Runner) Describe() []Command { return r.manifest.Commands }

// Run executes the named fixture command, using a cached result when one
// exists for this exact command+args. useCache=false forces a fresh run.
func (r *Runner) Run(ctx context.Context, name string, useCache bool) (*Result, error) {
	cmd, ok := r.manifest.Lookup(name)
	if !ok {
		return nil, fmt.Errorf("unknown fixture command %q; allowed: %v", name, r.manifest.Names())
	}
	cachePath := r.cachePath(cmd)

	if useCache {
		if cached, err := readCache(cachePath); err == nil {
			cached.Cached = true
			return cached, nil
		}
	}

	timeout := r.defaultTimeout
	if cmd.TimeoutSeconds > 0 {
		timeout = time.Duration(cmd.TimeoutSeconds) * time.Second
	}
	runCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	start := time.Now()
	execCmd := exec.CommandContext(runCtx, cmd.resolved, cmd.Args...)
	cwd, err := r.resolveWorkDir(cmd)
	if err != nil {
		return nil, err
	}
	execCmd.Dir = cwd
	// Minimal, deterministic environment. Fixtures are local only.
	execCmd.Env = []string{
		"PATH=" + os.Getenv("PATH"),
		"HOME=" + os.Getenv("HOME"),
		"TZ=UTC",
	}

	out, err := execCmd.CombinedOutput()
	duration := time.Since(start)

	res := &Result{
		Command:    cmd.Name,
		ExitCode:   0,
		Stdout:     string(out),
		DurationMS: duration.Milliseconds(),
		RanAt:      start.UTC().Format(time.RFC3339),
	}
	if err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			res.ExitCode = exitErr.ExitCode()
			res.Stderr = string(exitErr.Stderr)
		} else if errors.Is(runCtx.Err(), context.DeadlineExceeded) {
			res.ExitCode = 124
			res.Error = fmt.Sprintf("timeout after %s", timeout)
		} else {
			res.ExitCode = -1
			res.Error = err.Error()
		}
	}

	// Only successful runs are cached.
	if res.ExitCode == 0 {
		if err := writeCache(cachePath, res); err != nil {
			res.Stderr += "\ncache write warning: " + err.Error()
		}
	}
	return res, nil
}

func (r *Runner) resolveWorkDir(c *Command) (string, error) {
	if c.WorkDir == "" {
		return r.workDir, nil
	}
	dir := c.WorkDir
	if !filepath.IsAbs(dir) {
		dir = filepath.Join(r.baseDir, dir)
	}
	dir = filepath.Clean(dir)
	// Confine custom work directories to the project base.
	rel, err := filepath.Rel(r.baseDir, dir)
	if err != nil || rel == ".." || filepath.IsAbs(rel) || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("fixture %q: workdir %q escapes the base directory", c.Name, c.WorkDir)
	}
	if info, err := os.Stat(dir); err != nil || !info.IsDir() {
		return "", fmt.Errorf("fixture %q: workdir %q is not an existing directory", c.Name, c.WorkDir)
	}
	return dir, nil
}

func (r *Runner) cachePath(c *Command) string {
	key := c.resolved
	for _, a := range c.Args {
		key += "\x00" + a
	}
	sum := sha256.Sum256([]byte(key))
	return filepath.Join(r.cacheDir, "fixture-"+hex.EncodeToString(sum[:12])+".json")
}

func readCache(path string) (*Result, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var r Result
	if err := json.Unmarshal(data, &r); err != nil {
		return nil, err
	}
	return &r, nil
}

func writeCache(path string, r *Result) error {
	data, err := json.MarshalIndent(r, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
