// Package builder implements the local build service: it materializes a
// source tree into an isolated per-build working directory, runs exactly the
// fixture commands supplied in the request (no network, no shell, fixed
// environment), then packs the result into a deterministic tar artifact.
//
// The cache directory and the working directory are strictly separate:
//
//   - WorkDir holds per-build ephemeral trees ("work/<id>");
//   - CacheDir holds content-addressed read-only artifacts ("cache/<sha>");
//
// Two builds with identical results share one cache file; the source trees'
// mtimes and readdir order never affect the artifact bytes.
package builder

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"reprobuild/internal/archive"
)

// Fixture is one explicitly provided test-fixture command.
type Fixture struct {
	Name string   `json:"name"`
	Args []string `json:"args"`
	// Env are extra KEY=VALUE variables for this command.
	Env map[string]string `json:"env,omitempty"`
	// TimeoutSeconds bounds the command; 0 uses the service default.
	TimeoutSeconds int `json:"timeout_seconds,omitempty"`
}

// InlineFile creates or overwrites one file in the working tree before the
// fixtures run.
type InlineFile struct {
	Path    string `json:"path"`
	Content string `json:"content"`
	Mode    uint32 `json:"mode,omitempty"` // 0 => 0644
}

// BuildRequest is the JSON body of POST /api/v1/builds.
type BuildRequest struct {
	// SourceDir is a local path copied into the isolated work directory.
	// Optional when Files are supplied.
	SourceDir string `json:"source_dir,omitempty"`
	// Files are materialized on top of the copied source (or alone).
	Files []InlineFile `json:"files,omitempty"`
	// Fixtures are the ONLY commands ever executed.
	Fixtures []Fixture `json:"fixtures,omitempty"`
	// KeepWork retains the ephemeral work directory after packing.
	KeepWork bool `json:"keep_work,omitempty"`
	// FixedTimeSeconds pins archive entry mtimes; zero means Unix epoch.
	FixedTimeSeconds int64 `json:"fixed_time_seconds,omitempty"`
}

// FixtureResult reports one fixture execution.
type FixtureResult struct {
	Name           string   `json:"name"`
	Args           []string `json:"args"`
	ExitCode       int      `json:"exit_code"`
	Stdout         string   `json:"stdout,omitempty"`
	Stderr         string   `json:"stderr,omitempty"`
	DurationMillis int64    `json:"duration_millis"`
	Skipped        bool     `json:"skipped,omitempty"`
}

// Artifact describes the produced tar.
type Artifact struct {
	Sha256  string `json:"sha256"`
	Size    int64  `json:"size"`
	Path    string `json:"path"`
	Entries int    `json:"entries"`
}

// Record is the persisted result of a build.
type Record struct {
	ID          string          `json:"id"`
	Status      string          `json:"status"` // ok | fixture_failed | error
	Error       string          `json:"error,omitempty"`
	FixtureStep int             `json:"fixture_step,omitempty"`
	Fixtures    []FixtureResult `json:"fixtures"`
	Artifact    *Artifact       `json:"artifact,omitempty"`
	WorkDir     string          `json:"work_dir,omitempty"`
	CreatedAt   time.Time       `json:"created_at"`
}

// Config configures the Service.
type Config struct {
	WorkDir          string // ephemeral per-build trees
	CacheDir         string // content-addressed artifacts
	DataDir          string // persisted records index
	FixtureTimeout   time.Duration
	MaxFixtureOutput int64 // bytes of stdout+stderr captured per fixture
}

// Service is the local build service. It is safe for concurrent use.
type Service struct {
	cfg Config

	mu      sync.Mutex
	records map[string]*Record
}

// New creates a Service and ensures all directories exist.
func New(cfg Config) (*Service, error) {
	if cfg.WorkDir == "" || cfg.CacheDir == "" {
		return nil, errors.New("builder: work and cache directories are required")
	}
	if cfg.DataDir == "" {
		cfg.DataDir = filepath.Join(cfg.CacheDir, "..", "data")
	}
	if cfg.FixtureTimeout == 0 {
		cfg.FixtureTimeout = 60 * time.Second
	}
	if cfg.MaxFixtureOutput == 0 {
		cfg.MaxFixtureOutput = 64 * 1024
	}
	for _, d := range []string{cfg.WorkDir, cfg.CacheDir, cfg.DataDir} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, fmt.Errorf("builder: create %s: %w", d, err)
		}
	}
	s := &Service{cfg: cfg, records: make(map[string]*Record)}
	if err := s.loadIndex(); err != nil {
		return nil, err
	}
	return s, nil
}

// Build runs one build request.
func (s *Service) Build(ctx context.Context, req BuildRequest) *Record {
	id := newID()
	work := filepath.Join(s.cfg.WorkDir, id)
	rec := &Record{ID: id, Status: "error", CreatedAt: time.Now().UTC(), WorkDir: work}

	if err := os.MkdirAll(work, 0o755); err != nil {
		rec.Error = fmt.Sprintf("create work dir: %v", err)
		return s.finish(rec, req.KeepWork)
	}

	// 1. Materialize the source tree.
	if req.SourceDir != "" {
		if err := archive.ValidateTree(req.SourceDir); err != nil {
			rec.Error = err.Error()
			return s.finish(rec, req.KeepWork)
		}
		if err := copyTree(req.SourceDir, work); err != nil {
			rec.Error = fmt.Sprintf("copy source: %v", err)
			return s.finish(rec, req.KeepWork)
		}
	}
	for i := range req.Files {
		if err := writeInline(work, req.Files[i]); err != nil {
			rec.Error = fmt.Sprintf("write inline file: %v", err)
			return s.finish(rec, req.KeepWork)
		}
	}

	// 2. Run the explicitly provided fixtures, in order.
	rec.Fixtures = make([]FixtureResult, 0, len(req.Fixtures))
	for i, fx := range req.Fixtures {
		fr := s.runFixture(ctx, work, fx)
		rec.Fixtures = append(rec.Fixtures, fr)
		rec.FixtureStep = i
		if fr.ExitCode != 0 {
			rec.Status = "fixture_failed"
			return s.finish(rec, req.KeepWork)
		}
	}

	// 3. Pack deterministically. The tar is staged OUTSIDE the work tree so
	// the staging file itself can never become an archive entry.
	stageDir, err := os.MkdirTemp(s.cfg.WorkDir, ".stage-")
	if err != nil {
		rec.Error = fmt.Sprintf("create stage dir: %v", err)
		return s.finish(rec, req.KeepWork)
	}
	defer os.RemoveAll(stageDir)
	opts := &archive.Options{}
	if req.FixedTimeSeconds != 0 {
		opts.FixedTime = time.Unix(req.FixedTimeSeconds, 0).UTC()
	}
	if err := archive.ValidateTree(work); err != nil {
		rec.Error = err.Error()
		return s.finish(rec, req.KeepWork)
	}
	tmpTar := filepath.Join(stageDir, "artifact.tar")
	f, err := os.OpenFile(tmpTar, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		rec.Error = fmt.Sprintf("create artifact: %v", err)
		return s.finish(rec, req.KeepWork)
	}
	n, sum, err := archive.WriteTarHashed(f, work, opts)
	clErr := f.Close()
	if err != nil {
		rec.Error = fmt.Sprintf("pack artifact: %v", err)
		return s.finish(rec, req.KeepWork)
	}
	if clErr != nil {
		rec.Error = fmt.Sprintf("close artifact: %v", clErr)
		return s.finish(rec, req.KeepWork)
	}
	fi, err := os.Stat(tmpTar)
	if err != nil {
		rec.Error = fmt.Sprintf("stat artifact: %v", err)
		return s.finish(rec, req.KeepWork)
	}

	// 4. ...then commit it into the separate, content-addressed cache.
	cachePath := filepath.Join(s.cfg.CacheDir, sum+".tar")
	if err := commitToCache(tmpTar, cachePath); err != nil {
		rec.Error = fmt.Sprintf("cache artifact: %v", err)
		return s.finish(rec, req.KeepWork)
	}
	rec.Artifact = &Artifact{Sha256: sum, Size: fi.Size(), Path: cachePath, Entries: n}
	rec.Status = "ok"
	return s.finish(rec, req.KeepWork)
}

func (s *Service) finish(rec *Record, keepWork bool) *Record {
	if !keepWork && rec.Status == "ok" {
		// Successful builds: remove the ephemeral tree; cache is authoritative.
		_ = os.RemoveAll(rec.WorkDir)
		rec.WorkDir = ""
	}
	s.mu.Lock()
	s.records[rec.ID] = rec
	s.mu.Unlock()
	_ = s.appendIndex(rec)
	return rec
}

// Get returns a recorded build.
func (s *Service) Get(id string) (*Record, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.records[id]
	return r, ok
}

// OpenArtifact opens the cached tar of a build for streaming.
func (s *Service) OpenArtifact(id string) (*os.File, *Artifact, error) {
	rec, ok := s.Get(id)
	if !ok {
		return nil, nil, ErrNotFound
	}
	if rec.Artifact == nil {
		return nil, nil, errors.New("build produced no artifact")
	}
	f, err := os.Open(rec.Artifact.Path)
	if err != nil {
		return nil, nil, err
	}
	return f, rec.Artifact, nil
}

// ErrNotFound is returned for unknown build IDs.
var ErrNotFound = errors.New("build not found")

// runFixture executes one fixture command directly (exec, never a shell).
func (s *Service) runFixture(ctx context.Context, work string, fx Fixture) FixtureResult {
	res := FixtureResult{Name: fx.Name, Args: fx.Args}
	if len(fx.Args) == 0 {
		res.ExitCode = -1
		res.Stderr = "fixture has empty command args"
		return res
	}
	timeout := s.cfg.FixtureTimeout
	if fx.TimeoutSeconds > 0 {
		timeout = time.Duration(fx.TimeoutSeconds) * time.Second
	}
	cctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	cmd := exec.CommandContext(cctx, fx.Args[0], fx.Args[1:]...)
	cmd.Dir = work
	cmd.Env = fixtureEnv(fx.Env)

	var stdout, stderr cappedBuffer
	stdout.cap = s.cfg.MaxFixtureOutput
	stderr.cap = s.cfg.MaxFixtureOutput
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	start := time.Now()
	err := cmd.Run()
	res.DurationMillis = time.Since(start).Milliseconds()
	res.Stdout = stdout.String()
	res.Stderr = stderr.String()
	if err != nil {
		switch {
		case errors.Is(cctx.Err(), context.DeadlineExceeded):
			res.ExitCode = 124
			if res.Stderr != "" {
				res.Stderr += "\n"
			}
			res.Stderr += "fixture timed out"
		default:
			var ee *exec.ExitError
			if errors.As(err, &ee) {
				res.ExitCode = ee.ExitCode()
			} else {
				res.ExitCode = 127
				if res.Stderr != "" {
					res.Stderr += "\n"
				}
				res.Stderr += err.Error()
			}
		}
	}
	return res
}

// fixtureEnv builds a minimal, deterministic environment. Only a small
// whitelist is inherited from the host; PATH is required to resolve fixture
// binaries and defaults to the standard system path.
func fixtureEnv(extra map[string]string) []string {
	allow := map[string]bool{
		"PATH": true, "LANG": true, "LANGUAGE": true, "LC_ALL": true,
		"LC_CTYPE": true, "TZ": true, "HOME": true, "TMPDIR": true,
	}
	seen := map[string]bool{}
	var env []string
	for _, key := range []string{"PATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ", "HOME", "TMPDIR"} {
		if v, ok := os.LookupEnv(key); ok && allow[key] {
			env = append(env, key+"="+v)
			seen[key] = true
		}
	}
	if !seen["PATH"] {
		env = append(env, "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
	}
	if !seen["TZ"] {
		env = append(env, "TZ=UTC")
	}
	keys := make([]string, 0, len(extra))
	for k := range extra {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		env = append(env, k+"="+extra[k])
	}
	return env
}

// cappedBuffer is an io.Writer that keeps at most cap bytes.
type cappedBuffer struct {
	bytes.Buffer
	cap int64
}

func (c *cappedBuffer) Write(p []byte) (int, error) {
	remaining := c.cap - int64(c.Buffer.Len())
	if remaining <= 0 {
		return len(p), nil // discard, but pretend consumed
	}
	if int64(len(p)) > remaining {
		p = p[:remaining]
	}
	return c.Buffer.Write(p)
}

// writeInline creates a file under root after rejecting path traversal.
func writeInline(root string, f InlineFile) error {
	if f.Path == "" || filepath.IsAbs(f.Path) {
		return fmt.Errorf("inline path %q must be a non-empty relative path", f.Path)
	}
	clean := filepath.Clean(f.Path)
	if clean == ".." || strings.HasPrefix(clean, ".."+string(os.PathSeparator)) {
		return fmt.Errorf("inline path %q escapes work directory", f.Path)
	}
	mode := os.FileMode(f.Mode)
	if mode == 0 {
		mode = 0o644
	}
	dst := filepath.Join(root, clean)
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	return os.WriteFile(dst, []byte(f.Content), mode)
}

// copyTree copies src into dst preserving the tree shape, file bytes,
// permission bits and symlinks (symlinks are copied as links, never
// followed). ValidateTree must have run on src first.
func copyTree(src, dst string) error {
	return filepath.WalkDir(src, func(path string, de os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, rerr := filepath.Rel(src, path)
		if rerr != nil {
			return rerr
		}
		target := dst
		if rel != "." {
			target = filepath.Join(dst, rel)
		}
		info, ierr := de.Info()
		if ierr != nil {
			return ierr
		}
		mode := info.Mode()
		switch {
		case mode.IsDir():
			return os.MkdirAll(target, mode.Perm()|0o700)
		case mode.IsRegular():
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			return copyFile(path, target, mode.Perm())
		case mode&os.ModeSymlink != 0:
			link, lerr := os.Readlink(path)
			if lerr != nil {
				return lerr
			}
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			_ = os.Remove(target)
			return os.Symlink(link, target)
		default:
			return fmt.Errorf("unsupported entry type in source tree: %s", rel)
		}
	})
}

func copyFile(src, dst string, perm os.FileMode) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, perm)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// commitToCache places the artifact at cachePath, reusing an identical
// existing cache entry. It is atomic for same-filesystem installs.
func commitToCache(src, cachePath string) error {
	if _, err := os.Stat(cachePath); err == nil {
		return os.Remove(src) // identical content already cached
	}
	if err := os.MkdirAll(filepath.Dir(cachePath), 0o755); err != nil {
		return err
	}
	if err := os.Link(src, cachePath); err == nil {
		return os.Remove(src)
	}
	// Cross-device fallback: copy.
	if err := copyFile(src, cachePath, 0o444); err != nil {
		return err
	}
	return os.Remove(src)
}

// ---- record index (append-only JSON lines, rebuilt into memory) ----

func (s *Service) indexPath() string { return filepath.Join(s.cfg.DataDir, "records.jsonl") }

func (s *Service) loadIndex() error {
	s.records = make(map[string]*Record)
	b, err := os.ReadFile(s.indexPath())
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	for _, line := range bytes.Split(b, []byte("\n")) {
		line = bytes.TrimSpace(line)
		if len(line) == 0 {
			continue
		}
		var r Record
		if err := json.Unmarshal(line, &r); err != nil {
			return fmt.Errorf("builder: corrupt record index: %w", err)
		}
		s.records[r.ID] = &r
	}
	return nil
}

func (s *Service) appendIndex(r *Record) error {
	line, err := json.Marshal(r)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(s.indexPath(), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = f.Write(append(line, '\n'))
	return err
}

// newID derives a short unique build ID from time + randomness.
func newID() string {
	var b [16]byte
	h := sha256.New()
	fmt.Fprintf(h, "%d", time.Now().UnixNano())
	if rf, err := os.Open("/dev/urandom"); err == nil {
		io.ReadFull(rf, b[:])
		rf.Close()
		h.Write(b[:])
	}
	return hex.EncodeToString(h.Sum(nil))[:16]
}
