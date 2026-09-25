// Package executor runs the small set of user-supplied *test fixture*
// commands used as build actions. It is deliberately restrictive:
//
//   - only a single configured interpreter binary (bash) is ever exec'd;
//   - the script argument must resolve (after symlink evaluation) to a file
//     inside the configured fixtures directory;
//   - no other program from PATH can be invoked as the command itself
//     (scripts of course may call the ordinary coreutils available to bash);
//   - the work directory is private per invocation and is removed afterwards;
//   - only an explicit allowlist of environment variables is inherited.
//
// Nothing here ever contacts a network or a cloud service.
package executor

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"buildprovenance/internal/provenance"
)

// DefaultInterpreter is the only program the service will exec directly.
const DefaultInterpreter = "/usr/bin/bash"

// InheritedEnvVars are passed through from the service environment if set.
var InheritedEnvVars = []string{"LANG", "LC_ALL", "TZ"}

// Policy validates declared commands against the fixture directory.
type Policy struct {
	Interpreter string
	FixtureDir  string // absolute, symlink-resolved
}

// ResolvedCommand is a validated command with the hashed inputs that feed the
// tool digest.
type ResolvedCommand struct {
	Interpreter       string
	InterpreterDigest provenance.Digest
	Script            string // absolute resolved fixture script path ("" if none)
	ScriptDigest      provenance.Digest
	Argv              []string // command exactly as declared (argv[0] = interpreter)
}

var (
	interpreterRe = regexp.MustCompile(`^bash$|^/usr/bin/bash$`)
	slotRe        = regexp.MustCompile(`^[A-Z][A-Z0-9_]{0,31}$`)
)

// NewPolicy creates a policy, resolving symlinks for both interpreter and
// fixture directory so containment cannot be bypassed with a symlink.
func NewPolicy(interpreter, fixtureDir string) (*Policy, error) {
	if interpreter == "" {
		interpreter = DefaultInterpreter
	}
	if !interpreterRe.MatchString(interpreter) {
		return nil, fmt.Errorf("executor: interpreter %q not permitted (only bash fixtures are allowed)", interpreter)
	}
	resolvedInterp, err := filepath.EvalSymlinks(interpreter)
	if err != nil {
		return nil, fmt.Errorf("executor: interpreter %s: %w", interpreter, err)
	}
	absFixture, err := filepath.Abs(fixtureDir)
	if err != nil {
		return nil, err
	}
	resolvedFixture, err := filepath.EvalSymlinks(absFixture)
	if err != nil {
		return nil, fmt.Errorf("executor: fixture dir %s: %w", fixtureDir, err)
	}
	fi, err := os.Stat(resolvedFixture)
	if err != nil {
		return nil, err
	}
	if !fi.IsDir() {
		return nil, fmt.Errorf("executor: fixture dir %s is not a directory", resolvedFixture)
	}
	return &Policy{Interpreter: resolvedInterp, FixtureDir: resolvedFixture}, nil
}

// Resolve validates a declared command argv and hashes the interpreter and
// (when present) the fixture script.
func (p *Policy) Resolve(argv []string) (*ResolvedCommand, error) {
	if len(argv) == 0 {
		return nil, errors.New("executor: empty command")
	}
	if !interpreterRe.MatchString(argv[0]) {
		return nil, fmt.Errorf("executor: command[0]=%q is not the permitted interpreter", argv[0])
	}
	rc := &ResolvedCommand{Interpreter: p.Interpreter, Argv: append([]string{p.Interpreter}, argv[1:]...)}

	ib, err := os.ReadFile(p.Interpreter)
	if err != nil {
		return nil, fmt.Errorf("executor: read interpreter: %w", err)
	}
	rc.InterpreterDigest = provenance.DigestBytes(ib)

	if len(argv) >= 2 {
		scriptArg := argv[1]
		if filepath.IsAbs(scriptArg) || strings.Contains(scriptArg, "..") {
			return nil, fmt.Errorf("executor: fixture script must be a relative path without '..' components, got %q", scriptArg)
		}
		joined := filepath.Join(p.FixtureDir, scriptArg)
		resolved, err := filepath.EvalSymlinks(joined)
		if err != nil {
			return nil, fmt.Errorf("executor: fixture script %q: %w", scriptArg, err)
		}
		rel, err := filepath.Rel(p.FixtureDir, resolved)
		if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			return nil, fmt.Errorf("executor: fixture script %q escapes fixture directory", scriptArg)
		}
		fi, err := os.Stat(resolved)
		if err != nil {
			return nil, err
		}
		if fi.IsDir() {
			return nil, fmt.Errorf("executor: fixture script %q is a directory", scriptArg)
		}
		sb, err := os.ReadFile(resolved)
		if err != nil {
			return nil, err
		}
		rc.Script = resolved
		rc.ScriptDigest = provenance.DigestBytes(sb)
		// Rewrite argv[1] to the resolved absolute path for execution.
		rc.Argv = append([]string{p.Interpreter, resolved}, argv[2:]...)
	}
	return rc, nil
}

// MaterializedInput is an input placed into the work directory.
type MaterializedInput struct {
	Slot   string
	Path   string // absolute path inside the work dir
	Digest provenance.Digest
	Kind   string // "source" | "artifact"
	Ref    string // source path or artifact ID
}

// RunOptions configures one action execution.
type RunOptions struct {
	WorkRoot    string // parent directory for private work dirs
	Env         map[string]string
	Inputs      []MaterializedInput
	Outputs     []string // output slot names
	Timeout     time.Duration
	KeepWorkDir bool // retain work dir after run (debugging)
}

// Output is one produced output file.
type Output struct {
	Slot string
	Path string
}

// Result is the outcome of one execution.
type Result struct {
	WorkDir string
	Outputs []Output
	Stdout  string
	Stderr  string
}

// ValidateSlot checks input/output slot naming.
func ValidateSlot(s string) error {
	if !slotRe.MatchString(s) {
		return fmt.Errorf("invalid slot name %q: must match %s", s, slotRe.String())
	}
	return nil
}

// Run executes rc with inputs materialized under a fresh private work
// directory and OUT_<slot> environment variables pointing at an outputs
// subdirectory. The work directory is separate from all caches and removed on
// success (retained on failure / when KeepWorkDir is set).
func (p *Policy) Run(ctx context.Context, rc *ResolvedCommand, opts RunOptions) (*Result, error) {
	for _, in := range opts.Inputs {
		if err := ValidateSlot(in.Slot); err != nil {
			return nil, fmt.Errorf("input slot: %w", err)
		}
	}
	for _, s := range opts.Outputs {
		if err := ValidateSlot(s); err != nil {
			return nil, fmt.Errorf("output slot: %w", err)
		}
	}

	workDir, err := os.MkdirTemp(opts.WorkRoot, "build-")
	if err != nil {
		return nil, fmt.Errorf("executor: work dir: %w", err)
	}
	// On success the caller must remove the work dir (it reads the output
	// files from Result); on an ordinary command failure we clean up here.
	// Missing-output failures retain the dir for diagnostics.
	removeOnReturn := false
	defer func() {
		if removeOnReturn && !opts.KeepWorkDir {
			_ = os.RemoveAll(workDir)
		}
	}()

	inDir := filepath.Join(workDir, "in")
	outDir := filepath.Join(workDir, "out")
	if err := os.MkdirAll(inDir, 0o755); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(outDir, 0o755); err != nil {
		return nil, err
	}

	// Materialize each input as in/<SLOT>/content.
	for _, in := range opts.Inputs {
		d := filepath.Join(inDir, in.Slot)
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, err
		}
		target := filepath.Join(d, "content")
		if err := copyFile(target, in.Path); err != nil {
			return nil, fmt.Errorf("materialize %s: %w", in.Slot, err)
		}
	}

	env := baseEnv()
	for _, k := range InheritedEnvVars {
		if v, ok := os.LookupEnv(k); ok {
			env = append(env, k+"="+v)
		}
	}
	// Static tool env (sorted for determinism of the surrounding system;
	// the values themselves are part of the tool digest at registration).
	keys := make([]string, 0, len(opts.Env))
	for k := range opts.Env {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		env = append(env, k+"="+opts.Env[k])
	}
	for _, in := range opts.Inputs {
		env = append(env, "IN_"+in.Slot+"="+filepath.Join(inDir, in.Slot, "content"))
	}
	for _, s := range opts.Outputs {
		env = append(env, "OUT_"+s+"="+filepath.Join(outDir, s))
	}

	if opts.Timeout <= 0 {
		opts.Timeout = 30 * time.Second
	}
	runCtx, cancel := context.WithTimeout(ctx, opts.Timeout)
	defer cancel()

	cmd := exec.CommandContext(runCtx, rc.Argv[0], rc.Argv[1:]...)
	cmd.Dir = workDir
	cmd.Env = env
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	runErr := cmd.Run()
	res := &Result{WorkDir: workDir, Stdout: stdout.String(), Stderr: stderr.String()}

	var outputs []Output
	for _, s := range opts.Outputs {
		op := filepath.Join(outDir, s)
		if fi, statErr := os.Stat(op); statErr == nil && !fi.IsDir() {
			outputs = append(outputs, Output{Slot: s, Path: op})
		}
	}
	res.Outputs = outputs

	if runErr != nil {
		if errors.Is(runCtx.Err(), context.DeadlineExceeded) {
			return res, fmt.Errorf("executor: command timed out after %s; stderr: %s", opts.Timeout, stderr.String())
		}
		removeOnReturn = true
		return res, fmt.Errorf("executor: command failed: %w; stderr: %s", runErr, stderr.String())
	}
	if len(outputs) != len(opts.Outputs) {
		missing := []string{}
		have := map[string]bool{}
		for _, o := range outputs {
			have[o.Slot] = true
		}
		for _, s := range opts.Outputs {
			if !have[s] {
				missing = append(missing, s)
			}
		}
		return res, fmt.Errorf("executor: missing declared outputs %v (work dir retained: %s)", missing, workDir)
	}
	return res, nil
}

// Copy a staged input file (already in a CAS-local staging area by caller)
// into the work dir.
func copyFile(dst, src string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	tmp, err := os.CreateTemp(filepath.Dir(dst), ".stage-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := tmp.ReadFrom(in); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, dst)
}

func baseEnv() []string {
	// Minimal, predictable environment. PATH is fixed so fixture scripts
	// find ordinary utilities without inheriting caller PATH pollution.
	return []string{"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
}
