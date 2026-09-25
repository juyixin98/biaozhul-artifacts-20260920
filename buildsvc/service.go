// Package buildsvc implements the local build engineering service.
//
// The service runs ONLY commands explicitly published by the operator in a
// fixture manifest (fixtures/manifest.json by default). API clients supply a
// fixture name and optional parameters; they can never supply command lines.
//
// Cache and work are kept separate:
//   - WorkDir holds per-job scratch directories (jobs/<id>/...).
//   - produced artifacts are pushed to the remote HTTP cache and may also be
//     copied to an ArtifactDir when configured.
package buildsvc

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"modelcache/client"
	"modelcache/digest"
)

// Fixture is one operator-approved command template.
type Fixture struct {
	Name        string   `json:"name"`
	Description string   `json:"description,omitempty"`
	Command     string   `json:"command"`             // absolute, or looked up in Dir / PATH
	Args        []string `json:"args"`                // fixed arguments
	Outputs     []string `json:"outputs"`             // paths (relative to work dir) produced on success
	Timeout     Duration `json:"timeout,omitempty"`   // per-fixture cap
	AllowFail   bool     `json:"allowFail,omitempty"` // non-zero exit still yields artifacts/logs
}

// Duration is a JSON-decodable time.Duration ("5s", "2m").
type Duration time.Duration

func (d Duration) Std() time.Duration { return time.Duration(d) }

func (d *Duration) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	dd, err := time.ParseDuration(s)
	if err != nil {
		return err
	}
	*d = Duration(dd)
	return nil
}

// Manifest is the operator-published allowlist.
type Manifest struct {
	Fixtures []Fixture `json:"fixtures"`
}

var fixtureNameRE = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)
var outputPathRE = regexp.MustCompile(`^[A-Za-z0-9._/-]+$`)

// LoadManifest reads and validates the fixture allowlist.
func LoadManifest(path string) (*Manifest, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("load fixture manifest: %w", err)
	}
	var m Manifest
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, fmt.Errorf("parse fixture manifest: %w", err)
	}
	if err := m.validate(); err != nil {
		return nil, err
	}
	// Scripts referenced via ${FIXTURES_DIR}/... resolve next to the manifest,
	// never relative to the per-job work directory.
	if err := m.ExpandPaths(filepath.Dir(path)); err != nil {
		return nil, err
	}
	return &m, nil
}

func (m *Manifest) validate() error {
	seen := map[string]bool{}
	for i := range m.Fixtures {
		f := &m.Fixtures[i]
		if !fixtureNameRE.MatchString(f.Name) {
			return fmt.Errorf("fixture %q: invalid name", f.Name)
		}
		if seen[f.Name] {
			return fmt.Errorf("fixture %q: duplicated", f.Name)
		}
		seen[f.Name] = true
		if strings.TrimSpace(f.Command) == "" {
			return fmt.Errorf("fixture %q: empty command", f.Name)
		}
		if strings.ContainsAny(f.Command, " \t\n") {
			return fmt.Errorf("fixture %q: command must be a single path, args go in args", f.Name)
		}
		for _, o := range f.Outputs {
			if filepath.IsAbs(o) || strings.Contains(o, "..") || !outputPathRE.MatchString(o) {
				return fmt.Errorf("fixture %q: unsafe output path %q", f.Name, o)
			}
		}
	}
	return nil
}

// ExpandPaths substitutes ${FIXTURES_DIR} in command and args with the
// manifest directory made absolute.
func (m *Manifest) ExpandPaths(absDir string) error {
	abs, err := filepath.Abs(absDir)
	if err != nil {
		return err
	}
	repl := func(s string) string { return strings.ReplaceAll(s, "${FIXTURES_DIR}", abs) }
	for i := range m.Fixtures {
		f := &m.Fixtures[i]
		f.Command = repl(f.Command)
		for j, a := range f.Args {
			f.Args[j] = repl(a)
		}
	}
	return nil
}

// Options configures the Service.
type Options struct {
	WorkDir      string // per-job scratch root
	ArtifactDir  string // optional local copy of produced artifacts (0644)
	Manifest     *Manifest
	Cache        *client.Client // remote cache client
	DefaultLimit time.Duration  // default job timeout
	MaxLimit     time.Duration  // upper bound on job timeout
	Logger       *log.Logger
}

// Artifact describes one build output after it has been cached.
type Artifact struct {
	Name   string        `json:"name"`
	Path   string        `json:"path"`
	Digest digest.Digest `json:"digest"`
	Size   int64         `json:"size"`
}

// Job state.
const (
	StatusQueued    = "queued"
	StatusRunning   = "running"
	StatusSucceeded = "succeeded"
	StatusFailed    = "failed"
)

// JobView is the JSON-serializable state of a job (no synchronization fields).
type JobView struct {
	ID        string     `json:"id"`
	Fixture   string     `json:"fixture"`
	Status    string     `json:"status"`
	ExitCode  *int       `json:"exitCode,omitempty"`
	Error     string     `json:"error,omitempty"`
	Artifacts []Artifact `json:"artifacts,omitempty"`
	CreatedAt time.Time  `json:"createdAt"`
	UpdatedAt time.Time  `json:"updatedAt"`
	LogsTail  string     `json:"logsTail,omitempty"`
	WorkDir   string     `json:"workDir,omitempty"`
}

// Job is the state of one build invocation.
type Job struct {
	ID        string
	Fixture   string
	Status    string
	ExitCode  *int
	Error     string
	Artifacts []Artifact
	CreatedAt time.Time
	UpdatedAt time.Time
	LogsTail  string
	WorkDir   string

	mu     sync.Mutex
	doneCh chan struct{}
}

// Done is closed once the job reaches a terminal state.
func (j *Job) Done() <-chan struct{} { return j.doneCh }

// Snapshot returns the job's observable state as a JSON-safe view.
func (j *Job) Snapshot() JobView {
	j.mu.Lock()
	defer j.mu.Unlock()
	v := JobView{
		ID:        j.ID,
		Fixture:   j.Fixture,
		Status:    j.Status,
		ExitCode:  j.ExitCode,
		Error:     j.Error,
		CreatedAt: j.CreatedAt,
		UpdatedAt: j.UpdatedAt,
		LogsTail:  j.LogsTail,
		WorkDir:   j.WorkDir,
	}
	if j.Artifacts != nil {
		v.Artifacts = append([]Artifact(nil), j.Artifacts...)
	}
	return v
}

// Service runs fixture-defined builds.
type Service struct {
	opts Options
	log  *log.Logger

	jobsMu sync.Mutex
	jobs   map[string]*Job

	queue chan *Job
	wg    sync.WaitGroup
}

// New constructs the Service and starts worker goroutines.
func New(opts Options) (*Service, error) {
	if opts.Manifest == nil {
		return nil, errors.New("buildsvc: manifest is required")
	}
	if opts.WorkDir == "" {
		return nil, errors.New("buildsvc: work dir is required")
	}
	if opts.Cache == nil {
		return nil, errors.New("buildsvc: cache client is required")
	}
	if opts.DefaultLimit <= 0 {
		opts.DefaultLimit = 5 * time.Minute
	}
	if opts.MaxLimit <= 0 {
		opts.MaxLimit = 30 * time.Minute
	}
	logger := opts.Logger
	if logger == nil {
		logger = log.Default()
	}
	for _, d := range []string{opts.WorkDir, filepath.Join(opts.WorkDir, "jobs"), opts.ArtifactDir} {
		if d == "" {
			continue
		}
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, fmt.Errorf("buildsvc: create %s: %w", d, err)
		}
	}
	s := &Service{
		opts:  opts,
		log:   logger,
		jobs:  map[string]*Job{},
		queue: make(chan *Job, 1024),
	}
	workers := 4
	for i := 0; i < workers; i++ {
		s.wg.Add(1)
		go s.worker(i)
	}
	return s, nil
}

// Close waits for running jobs and stops workers.
func (s *Service) Close() {
	close(s.queue)
	s.wg.Wait()
}

func (s *Service) fixture(name string) (*Fixture, bool) {
	for i := range s.opts.Manifest.Fixtures {
		if s.opts.Manifest.Fixtures[i].Name == name {
			return &s.opts.Manifest.Fixtures[i], true
		}
	}
	return nil, false
}

// StartBuild enqueues a fixture run. params currently carries no user-controlled
// command surface; it is reserved for fixture-defined substitution.
func (s *Service) StartBuild(fixtureName string, params map[string]string) (*Job, error) {
	if _, ok := s.fixture(fixtureName); !ok {
		return nil, fmt.Errorf("unknown fixture %q (allowed: %s)", fixtureName, s.fixtureNames())
	}
	id := newJobID()
	now := time.Now().UTC()
	j := &Job{ID: id, Fixture: fixtureName, Status: StatusQueued, CreatedAt: now, UpdatedAt: now, doneCh: make(chan struct{})}

	s.jobsMu.Lock()
	s.jobs[id] = j
	s.jobsMu.Unlock()

	s.queue <- j
	return j, nil
}

// Wait blocks until the job reaches a terminal state or ctx is cancelled.
func (j *Job) Wait(ctx context.Context) error {
	select {
	case <-j.doneCh:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// GetJob fetches a job by id.
func (s *Service) GetJob(id string) (*Job, bool) {
	s.jobsMu.Lock()
	defer s.jobsMu.Unlock()
	j, ok := s.jobs[id]
	return j, ok
}

// ListJobs returns jobs newest first.
func (s *Service) ListJobs() []*Job {
	s.jobsMu.Lock()
	defer s.jobsMu.Unlock()
	out := make([]*Job, 0, len(s.jobs))
	for _, j := range s.jobs {
		out = append(out, j)
	}
	sort.Slice(out, func(i, k int) bool { return out[i].CreatedAt.After(out[k].CreatedAt) })
	return out
}

// ListJobViews returns snapshot views, newest first.
func (s *Service) ListJobViews() []JobView {
	jobs := s.ListJobs()
	out := make([]JobView, 0, len(jobs))
	for _, j := range jobs {
		out = append(out, j.Snapshot())
	}
	return out
}

func (s *Service) fixtureNames() string {
	names := make([]string, 0, len(s.opts.Manifest.Fixtures))
	for _, f := range s.opts.Manifest.Fixtures {
		names = append(names, f.Name)
	}
	sort.Strings(names)
	return strings.Join(names, ", ")
}

func (s *Service) worker(n int) {
	defer s.wg.Done()
	for j := range s.queue {
		s.runJob(j)
	}
}

// runJob executes the fixture in an isolated work directory.
func (s *Service) runJob(j *Job) {
	ctx := context.Background()
	fx, _ := s.fixture(j.Fixture)

	jobDir := filepath.Join(s.opts.WorkDir, "jobs", j.ID)
	if err := os.MkdirAll(jobDir, 0o755); err != nil {
		s.finish(j, StatusFailed, -1, err, "", nil, jobDir)
		return
	}

	limit := s.opts.DefaultLimit
	if fx.Timeout.Std() > 0 {
		limit = fx.Timeout.Std()
	}
	if s.opts.MaxLimit > 0 && limit > s.opts.MaxLimit {
		limit = s.opts.MaxLimit
	}
	cctx, cancel := context.WithTimeout(ctx, limit)
	defer cancel()

	cmdPath, err := resolveCommand(fx.Command)
	if err != nil {
		s.finish(j, StatusFailed, -1, err, "", nil, jobDir)
		return
	}

	// Minimal, explicitly set environment. No inherited secrets.
	cmd := exec.CommandContext(cctx, cmdPath, fx.Args...)
	prepareCmd(cmd)
	cmd.Dir = jobDir
	cmd.Env = []string{
		"PATH=" + os.Getenv("PATH"),
		"HOME=" + os.Getenv("HOME"),
		"MODEL_CACHE_JOB_ID=" + j.ID,
	}
	// Kill the whole process group on timeout.
	cmd.Cancel = func() error { return killProcessGroup(cmd) }

	var logBuf tailBuffer
	logBuf.limit = 16 * 1024
	cmd.Stdout = &logBuf
	cmd.Stderr = &logBuf

	s.setState(j, StatusRunning, nil)
	s.log.Printf("job %s: running fixture %q in %s (timeout %s)", j.ID, j.Fixture, jobDir, limit)

	startErr := cmd.Start()
	if startErr != nil {
		s.finish(j, StatusFailed, -1, startErr, logBuf.String(), nil, jobDir)
		return
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

	// Collect declared outputs. Missing outputs are an error on success;
	// on failure they're just skipped.
	var arts []Artifact
	var collectErr error
	if waitErr == nil || fx.AllowFail {
		arts, collectErr = s.collectArtifacts(cctx, fx, jobDir)
	}

	timedOut := cctx.Err() == context.DeadlineExceeded
	switch {
	case waitErr == nil && collectErr == nil:
		s.finish(j, StatusSucceeded, exitCode, nil, logBuf.String(), arts, jobDir)
	case timedOut:
		s.finish(j, StatusFailed, exitCode, fmt.Errorf("fixture %q timed out after %s", fx.Name, limit), logBuf.String(), arts, jobDir)
	default:
		msg := fmt.Errorf("fixture %q exited with code %d", fx.Name, exitCode)
		if collectErr != nil && waitErr == nil {
			msg = collectErr
		}
		s.finish(j, StatusFailed, exitCode, msg, logBuf.String(), arts, jobDir)
	}
}

// collectArtifacts validates, copies (optional) and uploads declared outputs.
func (s *Service) collectArtifacts(ctx context.Context, fx *Fixture, jobDir string) ([]Artifact, error) {
	if len(fx.Outputs) == 0 {
		return nil, fmt.Errorf("fixture %q declares no outputs", fx.Name)
	}
	arts := make([]Artifact, 0, len(fx.Outputs))
	for _, rel := range fx.Outputs {
		// Defense in depth: clean and confine every output to jobDir.
		clean := filepath.Clean(rel)
		if filepath.IsAbs(clean) || strings.HasPrefix(clean, "..") {
			return nil, fmt.Errorf("unsafe output path %q", rel)
		}
		src := filepath.Join(jobDir, clean)
		if err := withinDir(jobDir, src); err != nil {
			return nil, err
		}
		fi, err := os.Stat(src)
		if err != nil {
			return nil, fmt.Errorf("expected output %q missing: %w", rel, err)
		}
		if fi.IsDir() {
			return nil, fmt.Errorf("output %q is a directory", rel)
		}
		pr, err := s.opts.Cache.PutFile(ctx, src)
		if err != nil {
			return nil, fmt.Errorf("cache upload %s: %w", rel, err)
		}
		arts = append(arts, Artifact{
			Name:   filepath.Base(rel),
			Path:   rel,
			Digest: pr.Digest,
			Size:   pr.Size,
		})
		if s.opts.ArtifactDir != "" {
			dst := filepath.Join(s.opts.ArtifactDir, pr.Digest.Hex())
			if err := copyFile(src, dst, 0o444); err != nil {
				return nil, fmt.Errorf("copy artifact: %w", err)
			}
		}
	}
	return arts, nil
}

func (s *Service) setState(j *Job, status string, exit *int) {
	j.mu.Lock()
	j.Status = status
	j.UpdatedAt = time.Now().UTC()
	if exit != nil {
		j.ExitCode = exit
	}
	j.mu.Unlock()
}

func (s *Service) finish(j *Job, status string, exitCode int, err error, logs string, arts []Artifact, jobDir string) {
	j.mu.Lock()
	j.Status = status
	ec := exitCode
	j.ExitCode = &ec
	if err != nil {
		j.Error = err.Error()
	}
	j.Artifacts = arts
	j.LogsTail = logs
	j.WorkDir = jobDir
	j.UpdatedAt = time.Now().UTC()
	j.mu.Unlock()
	close(j.doneCh)
	s.log.Printf("job %s: %s (exit=%d, artifacts=%d)%s",
		j.ID, status, exitCode, len(arts), orFail(err))
}

func orFail(err error) string {
	if err != nil {
		return ": " + err.Error()
	}
	return ""
}

// --- helpers ---

var shortIDLetters = []byte("abcdefghijklmnopqrstuvwxyz0123456789")

func newJobID() string {
	var b [16]byte
	now := time.Now().UnixNano()
	for i := range b {
		b[i] = shortIDLetters[int(now>>uint(i*4))%len(shortIDLetters)]
	}
	return string(b[:])
}

func resolveCommand(name string) (string, error) {
	if filepath.IsAbs(name) {
		if _, err := os.Stat(name); err != nil {
			return "", fmt.Errorf("fixture command %s: %w", name, err)
		}
		return name, nil
	}
	p, err := exec.LookPath(name)
	if err != nil {
		return "", fmt.Errorf("fixture command %q not found in PATH: %w", name, err)
	}
	return p, nil
}

func withinDir(base, target string) error {
	rel, err := filepath.Rel(base, target)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return fmt.Errorf("path %q escapes work directory", target)
	}
	return nil
}

func copyFile(src, dst string, mode os.FileMode) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	tmp := dst + ".tmp"
	out, err := os.OpenFile(tmp, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		os.Remove(tmp)
		return err
	}
	if err := out.Sync(); err != nil {
		out.Close()
		os.Remove(tmp)
		return err
	}
	if err := out.Close(); err != nil {
		os.Remove(tmp)
		return err
	}
	if err := os.Chmod(tmp, mode); err != nil {
		os.Remove(tmp)
		return err
	}
	return os.Rename(tmp, dst)
}

// tailBuffer keeps the last `limit` bytes of build logs. Safe for concurrent
// writes (the child's stdout and stderr writers run on separate goroutines).
type tailBuffer struct {
	mu    sync.Mutex
	buf   []byte
	limit int
}

func (t *tailBuffer) Write(p []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.buf = append(t.buf, p...)
	if t.limit > 0 && len(t.buf) > t.limit {
		t.buf = t.buf[len(t.buf)-t.limit:]
	}
	return len(p), nil
}

func (t *tailBuffer) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return string(t.buf)
}
