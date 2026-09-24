// Package builder turns a build request into a cache key, runs real
// compilations under a controlled environment, and verifies artifacts.
package builder

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
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

	"buildcache/internal/key"
	"buildcache/internal/store"
)

// SourceInput is one requested source file.
type SourceInput struct {
	// Path is the project-relative forward-slash path.
	Path string `json:"path"`
	// Optional is true when an absent file is legitimate (it still binds the
	// key, so a later-added file changes the key).
	Optional bool `json:"optional,omitempty"`
	// Inline is raw file content supplied directly with the request. When
	// non-nil it is used instead of reading from SourceRoot.
	Inline []byte `json:"inline_b64,omitempty"`
}

// Request is a build request (already decoded by the HTTP layer).
type Request struct {
	ToolchainName   string            `json:"toolchain"`
	VersionCommand  []string          `json:"version_command"`
	CommandTemplate []string          `json:"command"`
	Args            []string          `json:"args"`
	Target          map[string]string `json:"target"`
	Env             map[string]string `json:"env"`
	Sources         []SourceInput     `json:"sources"`
	Artifact        string            `json:"artifact"`
	TimeoutSeconds  int               `json:"timeout_seconds,omitempty"`
}

// Outcome is the result of a build request (hit or freshly built).
type Outcome struct {
	Key                   string                  `json:"key"`
	Hit                   bool                    `json:"hit"`
	Status                string                  `json:"status"`
	Attempt               int                     `json:"attempt"`
	ArtifactSHA           string                  `json:"artifact_sha,omitempty"`
	ArtifactSize          int64                   `json:"artifact_size,omitempty"`
	ExitCode              int                     `json:"exit_code,omitempty"`
	Stdout                string                  `json:"stdout,omitempty"`
	Stderr                string                  `json:"stderr,omitempty"`
	Error                 string                  `json:"error,omitempty"`
	ElapsedMS             int64                   `json:"elapsed_ms"`
	Quarantined           []store.QuarantineEvent `json:"quarantined,omitempty"`
	KeyMaterial           *key.Material           `json:"-"`
	CanonicalMaterialJSON []byte                  `json:"-"`
}

// Builder coordinates the store, the on-disk artifact store and execution.
type Builder struct {
	st         *store.Store
	dataDir    string
	blobsDir   string
	quarDir    string
	sourceRoot string

	// In-process waiters: key -> broadcast channel closed when that key's
	// building lease ends. Cross-process waiters fall back to polling.
	mu      sync.Mutex
	waiters map[string]chan struct{}
}

// New creates a Builder and prepares blob/quarantine directories.
func New(st *store.Store, dataDir, sourceRoot string) (*Builder, error) {
	blobs := filepath.Join(dataDir, "blobs")
	quar := filepath.Join(dataDir, "quarantine")
	for _, d := range []string{blobs, quar} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, err
		}
	}
	if sourceRoot == "" {
		return nil, errors.New("source root must not be empty")
	}
	abs, err := filepath.Abs(sourceRoot)
	if err != nil {
		return nil, err
	}
	if fi, err := os.Stat(abs); err != nil || !fi.IsDir() {
		return nil, fmt.Errorf("source root is not a directory: %s", abs)
	}
	return &Builder{st: st, dataDir: dataDir, blobsDir: blobs, quarDir: quar,
		sourceRoot: abs, waiters: map[string]chan struct{}{}}, nil
}

// BlobPath returns the on-disk path of a verified artifact.
func (b *Builder) BlobPath(rel string) string {
	return filepath.Join(b.blobsDir, filepath.Clean("/"+rel))
}

// QuarantineDir exposes the isolation directory (used by tests).
func (b *Builder) QuarantineDir() string { return b.quarDir }

// ComputeMaterial resolves sources and the toolchain and produces key
// material without running a build. Used by POST /audit/key.
func (b *Builder) ComputeMaterial(ctx context.Context, req *Request) (*key.Material, []byte, error) {
	if err := validateRequest(req); err != nil {
		return nil, nil, err
	}
	entries, err := b.resolveSources(req.Sources)
	if err != nil {
		return nil, nil, err
	}
	tc, err := b.summarizeToolchain(ctx, req)
	if err != nil {
		return nil, nil, err
	}
	m := &key.Material{
		Toolchain: tc,
		Args:      req.Args,
		Command:   req.CommandTemplate,
		Target:    req.Target,
		Env:       envMapToSorted(req.Env),
		Sources:   entries,
	}
	raw, cacheKey, err := key.Canonicalize(m)
	if err != nil {
		return nil, nil, err
	}
	_ = cacheKey
	return m, raw, nil
}

// KeyOnly canonicalizes already-resolved material and returns the key.
func KeyOnly(m *key.Material) ([]byte, string, error) { return key.Canonicalize(m) }

func validateRequest(req *Request) error {
	if req == nil {
		return errors.New("empty request")
	}
	if strings.TrimSpace(req.ToolchainName) == "" {
		return errors.New("toolchain is required")
	}
	if len(req.VersionCommand) == 0 {
		return errors.New("version_command is required")
	}
	if len(req.CommandTemplate) == 0 {
		return errors.New("command is required")
	}
	if strings.TrimSpace(req.Artifact) == "" {
		return errors.New("artifact (output path relative to work dir) is required")
	}
	if strings.Contains(req.Artifact, "..") || filepath.IsAbs(req.Artifact) {
		return errors.New("artifact path must be relative and stay in the work dir")
	}
	if len(req.Sources) == 0 {
		return errors.New("at least one source is required")
	}
	for k := range req.Target {
		if strings.TrimSpace(k) == "" {
			return errors.New("target key must not be empty")
		}
	}
	return nil
}

// resolveSources reads every requested file and hashes it. Inline content
// wins; otherwise the file is read under SourceRoot with strict path
// containment. Missing optional files are recorded as Present=false; a
// present zero-byte file is recorded as Present=true with the SHA-256 of
// empty input — the two cases hash differently in the canonical material.
func (b *Builder) resolveSources(srcs []SourceInput) ([]key.SourceEntry, error) {
	out := make([]key.SourceEntry, 0, len(srcs))
	rootV := b.sourceRoot + string(os.PathSeparator)
	for _, s := range srcs {
		rel, err := key.NormalizePath(s.Path)
		if err != nil {
			return nil, err
		}
		entry := key.SourceEntry{Path: rel}
		if s.Inline != nil {
			sum := sha256.Sum256(s.Inline)
			entry.Present, entry.Size, entry.Digest = true, int64(len(s.Inline)), hex.EncodeToString(sum[:])
			out = append(out, entry)
			continue
		}
		full := filepath.Join(b.sourceRoot, filepath.FromSlash(rel))
		if full != b.sourceRoot && !strings.HasPrefix(full, rootV) {
			return nil, fmt.Errorf("source path escapes source root: %q", rel)
		}
		data, err := os.ReadFile(full)
		switch {
		case err == nil:
			sum := sha256.Sum256(data)
			entry.Present, entry.Size, entry.Digest = true, int64(len(data)), hex.EncodeToString(sum[:])
		case os.IsNotExist(err) && s.Optional:
			// Present stays false, digest stays "": distinct from empty file.
		case os.IsNotExist(err):
			return nil, fmt.Errorf("required source missing: %q", rel)
		default:
			return nil, fmt.Errorf("read source %q: %w", rel, err)
		}
		out = append(out, entry)
	}
	return out, nil
}

// materialize writes sources into a fresh work directory and returns it.
func (b *Builder) materializeSources(workDir string, srcs []SourceInput, manifest []key.SourceEntry) error {
	byPath := map[string]SourceInput{}
	for _, s := range srcs {
		rel, _ := key.NormalizePath(s.Path)
		byPath[rel] = s
	}
	// Deterministic order.
	sort.Slice(manifest, func(i, j int) bool { return manifest[i].Path < manifest[j].Path })
	for _, m := range manifest {
		dir := filepath.Join(workDir, filepath.Dir(filepath.FromSlash(m.Path)))
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return err
		}
		var data []byte
		if in, ok := byPath[m.Path]; ok && in.Inline != nil {
			data = in.Inline
		} else if m.Present {
			d, err := os.ReadFile(filepath.Join(b.sourceRoot, filepath.FromSlash(m.Path)))
			if err != nil {
				return err
			}
			data = d
		}
		f, err := os.OpenFile(filepath.Join(workDir, filepath.FromSlash(m.Path)),
			os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
		if err != nil {
			return err
		}
		_, werr := f.Write(data)
		if cerr := f.Close(); werr == nil {
			werr = cerr
		}
		if werr != nil {
			return werr
		}
	}
	return nil
}

// summarizeToolchain really executes the version command and hashes the
// resolved executable. The version command runs with a minimal environment so
// ambient values cannot leak into toolchain identity.
func (b *Builder) summarizeToolchain(ctx context.Context, req *Request) (key.ToolchainSummary, error) {
	if len(req.VersionCommand) == 0 {
		return key.ToolchainSummary{}, errors.New("version_command is required")
	}
	cctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	cmd := exec.CommandContext(cctx, req.VersionCommand[0], req.VersionCommand[1:]...)
	cmd.Env = minimalEnv()
	out, err := cmd.Output()
	if err != nil {
		return key.ToolchainSummary{}, fmt.Errorf("toolchain version command %v failed: %w", req.VersionCommand, err)
	}
	binPath, lookErr := exec.LookPath(req.VersionCommand[0])
	tc := key.ToolchainSummary{
		Name:           req.ToolchainName,
		VersionCommand: append([]string(nil), req.VersionCommand...),
		VersionOutput:  strings.TrimSpace(string(out)),
	}
	if lookErr == nil {
		tc.BinaryPath = binPath
		if data, rerr := os.ReadFile(binPath); rerr == nil {
			sum := sha256.Sum256(data)
			tc.BinaryDigest = hex.EncodeToString(sum[:])
		}
	}
	return tc, nil
}

func envMapToSorted(em map[string]string) []key.EnvVar {
	out := make([]key.EnvVar, 0, len(em))
	for k, v := range em {
		out = append(out, key.EnvVar{Name: k, Value: v})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// minimalEnv is the small fixed base given to every child process. Declared
// build env is layered on top; nothing else from the server is inherited.
func minimalEnv() []string {
	return []string{
		"PATH=" + os.Getenv("PATH"),
		"HOME=" + os.Getenv("HOME"),
		"LANG=C.UTF-8",
		"GOCACHE=" + filepath.Join(os.TempDir(), "buildcache-gocache"),
		"GOTMPDIR=" + os.TempDir(),
		"GOTOOLCHAIN=local",
		"GOPROXY=off",
	}
}

// childEnv layers target variables and declared env over the minimal base.
// Declared values win. Only declared names are part of the key; the fixed
// base is identical for every build and therefore key-neutral.
func childEnv(target map[string]string, declared map[string]string) []string {
	env := append([]string(nil), minimalEnv()...)
	add := func(k, v string) { env = append(env, k+"="+v) }
	for k, v := range target {
		add(strings.ToUpper(k), v)
	}
	names := make([]string, 0, len(declared))
	for k := range declared {
		names = append(names, k)
	}
	sort.Strings(names)
	for _, k := range names {
		add(k, declared[k])
	}
	return env
}

func (b *Builder) registerWaiter(k string) chan struct{} {
	b.mu.Lock()
	defer b.mu.Unlock()
	ch, ok := b.waiters[k]
	if !ok {
		ch = make(chan struct{})
		b.waiters[k] = ch
	}
	return ch
}

func (b *Builder) releaseWaiters(k string) {
	b.mu.Lock()
	if ch, ok := b.waiters[k]; ok {
		close(ch)
		delete(b.waiters, k)
	}
	b.mu.Unlock()
}

// hashFile returns hex SHA-256 and size of a regular file.
func hashFile(p string) (string, int64, error) {
	f, err := os.Open(p)
	if err != nil {
		return "", 0, err
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(h.Sum(nil)), n, nil
}

// ensureArtifact verifies an existing succeeded entry against the blob on
// disk. On any mismatch the blob is physically moved into quarantine/ and the
// row moved to quarantined; the quarantine event is returned.
func (b *Builder) ensureArtifact(ctx context.Context, e *store.Entry) (*store.QuarantineEvent, error) {
	p := filepath.Join(b.blobsDir, filepath.Clean("/"+e.ArtifactPath))
	sum, size, err := hashFile(p)
	switch {
	case err == nil && sum == e.ArtifactSHA && size == e.ArtifactSize:
		return nil, nil
	}
	reason, detail, observed := "hash_mismatch", "", sum
	if err != nil {
		reason, detail, observed = "artifact_missing", err.Error(), ""
	} else if size != e.ArtifactSize {
		reason = "size_mismatch"
		detail = fmt.Sprintf("stored=%d actual=%d", e.ArtifactSize, size)
	}
	// Physically isolate whatever is (or isn't) there.
	if err == nil {
		dst := filepath.Join(b.quarDir, fmt.Sprintf("%d-%s-%s", time.Now().UnixNano(),
			e.Key, filepath.Base(e.ArtifactPath)))
		if rerr := os.Rename(p, dst); rerr == nil {
			detail = strings.TrimSpace(detail + " moved to " + filepath.Base(dst))
		}
	}
	q, qerr := b.st.Quarantine(ctx, e.Key, reason, detail, e.ArtifactSHA, observed)
	if qerr != nil {
		return nil, qerr
	}
	return q, nil
}
