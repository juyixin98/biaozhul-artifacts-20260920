// Package builder runs explicitly user-supplied fixture commands in a
// throwaway working directory that is kept strictly separate from the
// cache. The executor never invents, loads, or infers commands: it runs
// exactly the argv the caller supplied in the request.
package builder

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"localcache/api"
	"localcache/internal/cas"
)

// Executor runs fixture commands against a CAS.
type Executor struct {
	Store          *cas.Store
	WorkRoot       string // parent dir for per-build temp dirs; must not be inside the cache
	MaxTimeout     time.Duration
	DefaultTimeout time.Duration
	MaxCapture     int64 // per-stream stdout/stderr capture limit
}

// NewExecutor creates an Executor, ensuring WorkRoot exists.
func NewExecutor(store *cas.Store, workRoot string, maxTimeout time.Duration) (*Executor, error) {
	if store == nil {
		return nil, errors.New("builder: nil store")
	}
	absWork, err := filepath.Abs(workRoot)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(absWork, 0o755); err != nil {
		return nil, err
	}
	return &Executor{
		Store:          store,
		WorkRoot:       absWork,
		MaxTimeout:     maxTimeout,
		DefaultTimeout: 10 * time.Second,
		MaxCapture:     1 << 20, // 1 MiB per stream
	}, nil
}

// cleanRel rejects absolute paths and anything that escapes the workdir.
func cleanRel(name string) error {
	if name == "" {
		return errors.New("empty path")
	}
	if filepath.IsAbs(name) || strings.HasPrefix(name, "~") {
		return fmt.Errorf("path %q must be relative", name)
	}
	c := filepath.Clean(name)
	if c != name || c == ".." || strings.HasPrefix(c, "../") {
		return fmt.Errorf("path %q is not a clean relative path", name)
	}
	return nil
}

// Run executes req.Argv exactly as given, in a fresh temporary working
// directory under WorkRoot (never inside the cache). Inputs are
// materialized from the CAS, declared outputs are collected back into the
// CAS, and the working directory is deleted afterwards.
func (e *Executor) Run(ctx context.Context, req *api.BuildRequest) (*api.BuildResult, error) {
	if len(req.Argv) == 0 {
		return nil, errors.New("argv must not be empty")
	}
	for name, digest := range req.Inputs {
		if err := cleanRel(name); err != nil {
			return nil, fmt.Errorf("input: %w", err)
		}
		if err := cas.ValidateDigest(digest); err != nil {
			return nil, fmt.Errorf("input %q: %w", name, err)
		}
	}
	for _, name := range req.Outputs {
		if err := cleanRel(name); err != nil {
			return nil, fmt.Errorf("output: %w", err)
		}
	}

	workDir, err := os.MkdirTemp(e.WorkRoot, "build-*")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(workDir)

	// Materialize inputs from the cache into the working directory.
	for name, digest := range req.Inputs {
		rc, _, err := e.Store.Get(digest)
		if err != nil {
			return nil, fmt.Errorf("fetching input %q (%s): %w", name, digest, err)
		}
		dst := filepath.Join(workDir, name)
		if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
			rc.Close()
			return nil, err
		}
		werr := writeFile(dst, rc)
		rc.Close()
		if werr != nil {
			return nil, fmt.Errorf("writing input %q: %w", name, werr)
		}
	}

	timeout := e.DefaultTimeout
	if req.TimeoutMs > 0 {
		timeout = time.Duration(req.TimeoutMs) * time.Millisecond
	}
	if timeout > e.MaxTimeout {
		timeout = e.MaxTimeout
	}
	runCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	cmd := exec.CommandContext(runCtx, req.Argv[0], req.Argv[1:]...)
	cmd.Dir = workDir
	// Minimal, explicit environment; nothing is inherited silently.
	cmd.Env = []string{
		"PATH=" + os.Getenv("PATH"),
		"HOME=" + workDir,
		"TMPDIR=" + workDir,
	}
	// Kill the whole process group on timeout/cancel, so children of the
	// fixture command (e.g. "sh -c 'sleep 30'") cannot outlive it; bound
	// the wait for their inherited pipes with WaitDelay.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}
	cmd.WaitDelay = 2 * time.Second
	stdout := &cappedBuffer{limit: e.MaxCapture}
	stderr := &cappedBuffer{limit: e.MaxCapture}
	cmd.Stdout = stdout
	cmd.Stderr = stderr

	start := time.Now()
	runErr := cmd.Run()
	res := &api.BuildResult{
		Stdout:      stdout.String(),
		Stderr:      stderr.String(),
		StdoutTrunc: stdout.truncated,
		StderrTrunc: stderr.truncated,
		DurationMs:  time.Since(start).Milliseconds(),
	}
	var exitErr *exec.ExitError
	switch {
	case runErr == nil:
		res.ExitCode = 0
	case runCtx.Err() == context.DeadlineExceeded:
		res.TimedOut = true
		res.ExitCode = -1
	case errors.As(runErr, &exitErr):
		res.ExitCode = exitErr.ExitCode()
	default:
		return nil, fmt.Errorf("starting command %q: %w", req.Argv[0], runErr)
	}

	// Collect declared outputs into the cache.
	if len(req.Outputs) > 0 {
		res.Outputs = map[string]string{}
	}
	for _, name := range req.Outputs {
		path := filepath.Join(workDir, name)
		st, err := os.Stat(path)
		if err != nil {
			if res.ExitCode == 0 && !res.TimedOut {
				return nil, fmt.Errorf("declared output %q was not produced: %w", name, err)
			}
			continue // a failed/timed-out command may legitimately leave no outputs
		}
		if st.Size() > e.Store.MaxSize() {
			return nil, fmt.Errorf("output %q: %w", name, cas.ErrTooLarge)
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("reading output %q: %w", name, err)
		}
		digest := cas.DigestOf(data)
		if _, err := e.Store.Put(digest, bytes.NewReader(data)); err != nil {
			return nil, fmt.Errorf("caching output %q: %w", name, err)
		}
		res.Outputs[name] = digest
	}
	return res, nil
}

func writeFile(path string, r io.Reader) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return err
	}
	if _, err := io.Copy(f, r); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

// cappedBuffer captures a stream up to limit bytes and records truncation.
type cappedBuffer struct {
	buf       bytes.Buffer
	limit     int64
	truncated bool
}

func (c *cappedBuffer) Write(p []byte) (int, error) {
	if rem := c.limit - int64(c.buf.Len()); rem < int64(len(p)) {
		if rem > 0 {
			c.buf.Write(p[:rem])
		}
		c.truncated = true
		return len(p), nil
	}
	return c.buf.Write(p)
}

func (c *cappedBuffer) String() string { return c.buf.String() }
