package trmerge

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// CommandSpec is one explicit command the user supplied for one shard.
// The service never invents commands: only these are ever exec'd.
type CommandSpec struct {
	ShardID string   `json:"shard_id"`
	Command string   `json:"command"`            // passed to "bash -c"
	Args    []string `json:"args,omitempty"`     // optional extra argv (informational / future use)
	WorkDir string   `json:"work_dir,omitempty"` // optional per-shard override
	Timeout string   `json:"timeout,omitempty"`  // e.g. "30s", Go duration
}

// Runner supervises the explicit shard commands of one execute-mode run.
type Runner struct {
	store    *Store
	runID    string
	workRoot string
	specs    []CommandSpec

	mu       sync.Mutex
	procs    map[string]*shardProc
	wg       sync.WaitGroup
	doneOnce sync.Once
	doneCh   chan struct{}
}

// NewRunner validates specs and prepares per-shard work directories.
func NewRunner(store *Store, runID, workRoot string, specs []CommandSpec) (*Runner, error) {
	if len(specs) == 0 {
		return nil, fmt.Errorf("execute mode requires at least one shard command")
	}
	seen := map[string]bool{}
	for i, sp := range specs {
		if strings.TrimSpace(sp.ShardID) == "" {
			return nil, fmt.Errorf("command[%d]: empty shard_id", i)
		}
		if seen[sp.ShardID] {
			return nil, fmt.Errorf("duplicate command for shard %q", sp.ShardID)
		}
		seen[sp.ShardID] = true
		if strings.TrimSpace(sp.Command) == "" {
			return nil, fmt.Errorf("command[%d] (%s): empty command", i, sp.ShardID)
		}
		if sp.WorkDir != "" {
			abs, err := filepath.Abs(filepath.Clean(sp.WorkDir))
			if err != nil {
				return nil, err
			}
			if err := store.EnsureSeparateFromWork(abs); err != nil {
				return nil, fmt.Errorf("command %s: %w", sp.ShardID, err)
			}
		}
	}
	if err := store.EnsureSeparateFromWork(workRoot); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(workRoot, 0o755); err != nil {
		return nil, fmt.Errorf("create work root: %w", err)
	}
	return &Runner{
		store: store, runID: runID, workRoot: workRoot,
		specs:  specs,
		procs:  map[string]*shardProc{},
		doneCh: make(chan struct{}),
	}, nil
}

// Start launches every shard concurrently and returns immediately.
func (r *Runner) Start() {
	for _, sp := range r.specs {
		r.wg.Add(1)
		go r.runShard(sp)
	}
	go func() {
		r.wg.Wait()
		r.doneOnce.Do(func() { close(r.doneCh) })
	}()
}

// Done is closed once all shard processes have exited.
func (r *Runner) Done() <-chan struct{} { return r.doneCh }

type shardProc struct {
	cancel   context.CancelFunc
	signaled *bool // set true when Cancel() terminates this shard
}

// Cancel requests termination of all running shards (SIGTERM, then SIGKILL)
// and persists a run_cancel_requested event so cancellation survives replay.
func (r *Runner) Cancel() {
	_, _ = r.store.RequestCancel(r.runID)
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, p := range r.procs {
		*p.signaled = true
		p.cancel()
	}
}

// Wait blocks until shards finish, then appends finalize (auto mode).
func (r *Runner) Wait(autoFinalize bool) {
	<-r.doneCh
	if autoFinalize {
		_, _ = r.store.Finalize(r.runID)
	}
}

func (r *Runner) runShard(sp CommandSpec) {
	defer r.wg.Done()

	workDir := sp.WorkDir
	if workDir == "" {
		workDir = filepath.Join(r.workRoot, safeName(sp.ShardID))
		if err := os.MkdirAll(workDir, 0o755); err != nil {
			r.emitShardFinish(sp.ShardID, -1, StatusCrashed,
				fmt.Sprintf("create work dir: %v", err), nil)
			return
		}
	}

	var ctx context.Context
	var cancel context.CancelFunc
	if sp.Timeout != "" {
		d, err := time.ParseDuration(sp.Timeout)
		if err != nil {
			r.warn(sp.ShardID, fmt.Sprintf("invalid timeout %q: %v", sp.Timeout, err))
		} else {
			ctx, cancel = context.WithTimeout(context.Background(), d)
		}
	}
	if ctx == nil {
		ctx, cancel = context.WithCancel(context.Background())
	}
	// signaled is set true under r.mu when Cancel() terminates this shard;
	// timedOut is set by the deadline context. Read after cmd.Wait, once the
	// context goroutine has finished, so there is no data race.
	signaled := false
	r.mu.Lock()
	r.procs[sp.ShardID] = &shardProc{cancel: cancel, signaled: &signaled}
	r.mu.Unlock()
	defer cancel()

	// Only the explicit command from the request is ever started, via bash.
	cmd := exec.CommandContext(ctx, "bash", "-c", sp.Command)
	cmd.Dir = workDir
	cmd.Env = append(os.Environ(),
		"TR_RUN_ID="+r.runID,
		"TR_SHARD_ID="+sp.ShardID,
		"TR_CACHE_DIR="+r.store.CacheRoot(),
	)
	// Send SIGTERM on cancel rather than immediate kill; escalate after grace.
	cmd.Cancel = func() error {
		_ = cmd.Process.Signal(syscall.SIGTERM)
		go func() {
			time.Sleep(5 * time.Second)
			_ = cmd.Process.Signal(syscall.SIGKILL)
		}()
		return os.ErrProcessDone
	}

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		r.emitShardFinish(sp.ShardID, -1, StatusCrashed, err.Error(), nil)
		return
	}
	var stderr strings.Builder
	cmd.Stderr = &stderr

	// shard_started once the process is actually spawned.
	r.emit(Event{Type: EvShardStarted, ShardID: sp.ShardID})

	if err := cmd.Start(); err != nil {
		r.emitShardFinish(sp.ShardID, -1, StatusCrashed,
			fmt.Sprintf("start command: %v", err), nil)
		return
	}

	// Parse newline-delimited JSON events from stdout as they arrive.
	var parseWarnings []string
	sc := bufio.NewScanner(stdout)
	sc.Buffer(make([]byte, 1024*1024), 16*1024*1024)
	lineNo := 0
	for sc.Scan() {
		lineNo++
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		var ev Event
		if err := json.Unmarshal([]byte(line), &ev); err != nil {
			parseWarnings = append(parseWarnings,
				fmt.Sprintf("line %d: invalid JSON: %.120s", lineNo, line))
			continue
		}
		ev.ShardID = sp.ShardID
		if err := DecodeEvent(&ev); err != nil {
			parseWarnings = append(parseWarnings,
				fmt.Sprintf("line %d: %v", lineNo, err))
			continue
		}
		r.emit(ev)
	}
	if err := sc.Err(); err != nil {
		parseWarnings = append(parseWarnings, fmt.Sprintf("read stdout: %v", err))
	}

	waitErr := cmd.Wait()
	exitCode := 0
	if waitErr != nil {
		if ee, ok := waitErr.(*exec.ExitError); ok {
			exitCode = ee.ExitCode()
		} else {
			exitCode = -1
		}
	}

	// Emit parse warnings (deduplicated by merger).
	for _, w := range parseWarnings {
		r.warn(sp.ShardID, w)
	}

	// Determine executor outcome. Explicit cancel or timeout -> canceled;
	// the process's own nonzero exit -> crashed; exit zero -> completed.
	outcome := StatusCompleted
	errMsg := ""
	switch {
	case signaled:
		outcome = StatusCanceled
		errMsg = "canceled"
	case ctx.Err() == context.DeadlineExceeded:
		outcome = StatusCanceled
		errMsg = "timeout after " + sp.Timeout
	case waitErr != nil:
		outcome = StatusCrashed
		errMsg = waitErr.Error()
	}

	if e := strings.TrimSpace(stderr.String()); e != "" {
		if errMsg != "" {
			errMsg += "; "
		}
		errMsg += "stderr: " + truncate(e, 500)
	}

	r.emitShardFinish(sp.ShardID, exitCode, outcome, errMsg, parseWarnings)
}

func (r *Runner) emit(ev Event) {
	_, _ = r.store.Ingest(r.runID, []Event{ev})
}

func (r *Runner) warn(shardID, msg string) {
	r.emit(Event{Type: EvShardWarning, ShardID: shardID, Message: msg})
}

func (r *Runner) emitShardFinish(shardID string, exitCode int, outcome, errMsg string, _ []string) {
	ev := Event{
		Type: EvShardFinished, ShardID: shardID,
		Outcome: outcome, Error: errMsg,
	}
	if exitCode >= 0 {
		ev.ExitCode = &exitCode
	}
	r.emit(ev)
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

// safeName makes a shard id filesystem-safe for per-shard work directories.
func safeName(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_', r == '.':
			b.WriteRune(r)
		default:
			b.WriteByte('_')
		}
	}
	if b.Len() == 0 {
		return "shard"
	}
	return b.String()
}

var _ = strconv.Itoa
