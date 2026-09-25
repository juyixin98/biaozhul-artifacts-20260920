// Package executor runs user-provided fixture commands and parses their
// stdout as newline-delimited JSON events.
//
// Safety constraints:
//
//   - commands are never passed through a shell: argv is executed directly;
//   - the executable must live inside the configured fixtures root,
//     symlinks and "../" escapes are rejected;
//   - each execution gets its own directory under the work root, which is
//     deliberately separate from the event cache directory.
package executor

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/local/testmerge/internal/domain"
)

// IngestedEvent is one event accepted from fixture stdout.
type IngestedEvent struct {
	Event  domain.Event
	LineNo int `json:"line_no"`
}

// Record is the auditable result of one fixture execution.
type Record struct {
	RunID       string          `json:"run_id"`
	ExecutionID string          `json:"execution_id"`
	Command     []string        `json:"command"`
	FixturePath string          `json:"fixture_path"`
	WorkDir     string          `json:"work_dir"`
	Env         []string        `json:"env"`
	StartedAt   time.Time       `json:"started_at"`
	FinishedAt  time.Time       `json:"finished_at"`
	ExitCode    int             `json:"exit_code"`
	TimedOut    bool            `json:"timed_out"`
	KilledBySig bool            `json:"killed_by_signal"`
	StdoutLines int             `json:"stdout_lines"`
	StderrTail  string          `json:"stderr_tail"`
	Events      []IngestedEvent `json:"events"`
	ParseErrors []LineError     `json:"parse_errors"`
}

// LineError records a stdout line that was not a valid event.
type LineError struct {
	LineNo int    `json:"line_no"`
	Line   string `json:"line"`
	Error  string `json:"error"`
}

// Executor runs fixture commands.
type Executor struct {
	fixturesRoot string
	workRoot     string
}

// New validates and absolutizes the two roots.
func New(fixturesRoot, workRoot string) (*Executor, error) {
	fr, err := filepath.Abs(fixturesRoot)
	if err != nil {
		return nil, err
	}
	wr, err := filepath.Abs(workRoot)
	if err != nil {
		return nil, err
	}
	if info, err := os.Stat(fr); err != nil || info == nil || !info.IsDir() {
		return nil, fmt.Errorf("fixtures root %s is not a directory", fr)
	}
	if err := os.MkdirAll(wr, 0o755); err != nil {
		return nil, fmt.Errorf("create work root: %w", err)
	}
	return &Executor{fixturesRoot: fr, workRoot: wr}, nil
}

// FixturesRoot returns the resolved fixtures directory.
func (e *Executor) FixturesRoot() string { return e.fixturesRoot }

// WorkRoot returns the resolved work directory root.
func (e *Executor) WorkRoot() string { return e.workRoot }

// resolveFixture ensures name (no argv) is a regular executable inside the
// fixtures root, after symlink evaluation.
func (e *Executor) resolveFixture(name string) (string, error) {
	if name == "" {
		return "", errors.New("fixture name is required")
	}
	cleaned := filepath.Clean("/" + name) // forbid absolute user input
	joined := filepath.Join(e.fixturesRoot, cleaned)
	resolved, err := filepath.EvalSymlinks(joined)
	if err != nil {
		return "", fmt.Errorf("fixture %q not found under %s", name, e.fixturesRoot)
	}
	rootResolved, err := filepath.EvalSymlinks(e.fixturesRoot)
	if err != nil {
		return "", err
	}
	rel, err := filepath.Rel(rootResolved, resolved)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		return "", fmt.Errorf("fixture %q escapes fixtures root", name)
	}
	info, err := os.Stat(resolved)
	if err != nil {
		return "", err
	}
	if info.IsDir() {
		return "", fmt.Errorf("fixture %q is a directory", name)
	}
	if info.Mode()&0o111 == 0 {
		return "", fmt.Errorf("fixture %q is not executable", name)
	}
	return resolved, nil
}

// Run executes argv[0] (relative to the fixtures root) with argv[1:] args.
// The fixture runs with cwd workRoot/<runID>/<executionID>. Events printed
// on stdout are parsed; non-JSON lines are reported, not fatal.
func (e *Executor) Run(ctx context.Context, runID, executionID string, argv, env []string, timeout time.Duration) (*Record, error) {
	if len(argv) == 0 {
		return nil, errors.New("command argv is required")
	}
	bin, err := e.resolveFixture(argv[0])
	if err != nil {
		return nil, err
	}
	workDir := filepath.Join(e.workRoot, runID, executionID)
	if err := os.MkdirAll(workDir, 0o755); err != nil {
		return nil, fmt.Errorf("create work dir: %w", err)
	}

	runCtx := ctx
	var cancel context.CancelFunc
	if timeout > 0 {
		runCtx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}

	rec := &Record{
		RunID:       runID,
		ExecutionID: executionID,
		Command:     append([]string{bin}, argv[1:]...),
		FixturePath: bin,
		WorkDir:     workDir,
		Env:         append([]string(nil), env...),
		StartedAt:   time.Now().UTC(),
	}

	cmd := exec.CommandContext(runCtx, bin, argv[1:]...)
	cmd.Dir = workDir
	cmd.Env = append(os.Environ(), env...)
	// Give the fixture its own process group so a timeout kills the whole
	// tree, not just the wrapper shell (otherwise a child can inherit the
	// stdout pipe and keep cmd.Run blocked).
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		// Negative pid signals the whole group.
		return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}
	cmd.WaitDelay = 2 * time.Second
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	err = cmd.Run()
	rec.FinishedAt = time.Now().UTC()
	rec.StderrTail = tail(stderr.String(), 4096)

	if runCtx.Err() == context.DeadlineExceeded {
		rec.TimedOut = true
		rec.ExitCode = -1
	} else if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			rec.ExitCode = ee.ExitCode()
			rec.KilledBySig = ee.ExitCode() == -1
		} else {
			return rec, fmt.Errorf("execute fixture: %w", err)
		}
	}

	parseOutput(stdout.String(), rec)
	return rec, nil
}

func parseOutput(out string, rec *Record) {
	for i, line := range strings.Split(out, "\n") {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" {
			continue
		}
		rec.StdoutLines++
		lineNo := i + 1
		var ev domain.Event
		if err := json.Unmarshal([]byte(trimmed), &ev); err != nil {
			rec.ParseErrors = append(rec.ParseErrors, LineError{LineNo: lineNo, Line: trimmed, Error: err.Error()})
			continue
		}
		rec.Events = append(rec.Events, IngestedEvent{Event: ev, LineNo: lineNo})
	}
}

func tail(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return "…" + s[len(s)-n+1:]
}
