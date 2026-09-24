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
	"sort"
	"strings"
	"time"

	"buildcache/internal/key"
	"buildcache/internal/store"
)

// ErrConflict is returned when another publisher owns the key and the wait
// deadline passes.
var ErrConflict = errors.New("another build for this key is in progress")

// waitForCompletion polls the store until the entry leaves "building" or the
// deadline passes. In-process publishers also wake it via the waiter channel.
func (b *Builder) waitForCompletion(ctx context.Context, cacheKey string, deadline time.Time) (*store.Entry, error) {
	ch := b.registerWaiter(cacheKey)
	for {
		e, err := b.st.Get(ctx, cacheKey)
		if err != nil {
			return nil, err
		}
		if e.Status != store.StatusBuilding {
			return e, nil
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return e, ErrConflict
		}
		select {
		case <-ch:
			// publisher finished (or crashed); loop and re-read
		case <-time.After(minDuration(remaining, 200*time.Millisecond)):
			// cross-process safety net
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
}

func minDuration(a, b time.Duration) time.Duration {
	if a < b {
		return a
	}
	return b
}

// Build resolves a request into its key and either returns a verified hit or
// runs exactly one real compilation as the key's single publisher.
//
// wait bounds how long a non-publisher waits on an in-flight build.
func (b *Builder) Build(ctx context.Context, req *Request, wait time.Duration) (*Outcome, error) {
	if err := validateRequest(req); err != nil {
		return nil, &BadRequestError{err.Error()}
	}
	entries, err := b.resolveSources(req.Sources)
	if err != nil {
		return nil, &BadRequestError{err.Error()}
	}
	tc, err := b.summarizeToolchain(ctx, req)
	if err != nil {
		return nil, &BadRequestError{"toolchain summary failed: " + err.Error()}
	}
	material := &key.Material{
		Toolchain: tc,
		Args:      req.Args,
		Command:   req.CommandTemplate,
		Target:    req.Target,
		Env:       envMapToSorted(req.Env),
		Sources:   entries,
	}
	rawJSON, cacheKey, err := key.Canonicalize(material)
	if err != nil {
		return nil, &BadRequestError{err.Error()}
	}

	deadline := time.Now().Add(wait)
	for {
		// Look before we lease: a verified succeeded entry is served without
		// ever touching claim/rebuild semantics.
		e, err := b.st.Get(ctx, cacheKey)
		if err != nil && !errors.Is(err, store.ErrNotFound) {
			return nil, err
		}
		if err == nil {
			switch e.Status {
			case store.StatusSucceeded:
				q, verr := b.ensureArtifact(ctx, e)
				if verr != nil {
					return nil, verr
				}
				if q != nil {
					continue // corrupt entry isolated; fall through and rebuild
				}
				return hitOutcome(cacheKey, e, material, rawJSON, nil), nil
			case store.StatusBuilding:
				if time.Until(deadline) <= 0 {
					return nil, ErrConflict
				}
				e2, werr := b.waitForCompletion(ctx, cacheKey, deadline)
				if werr != nil {
					return nil, werr
				}
				if e2.Status == store.StatusSucceeded {
					q, verr := b.ensureArtifact(ctx, e2)
					if verr != nil {
						return nil, verr
					}
					if q != nil {
						continue
					}
					return hitOutcome(cacheKey, e2, material, rawJSON, nil), nil
				}
				// failed / quarantined / interrupted -> loop and rebuild.
				continue
			case store.StatusFailed, store.StatusQuarantined, store.StatusInterrupted:
				// fall through to claim/rebuild
			default:
				return nil, fmt.Errorf("unexpected entry status %q", e.Status)
			}
		}

		// No usable terminal entry: try to become the single publisher.
		claimed, ce, cerr := b.st.Claim(ctx, cacheKey)
		if cerr != nil {
			return nil, cerr
		}
		switch {
		case claimed:
			out := b.runAsPublisher(ctx, req, material, rawJSON, cacheKey, ce)
			b.releaseWaiters(cacheKey)
			return out, nil
		case ce.Status == store.StatusBuilding:
			// Lost a claim race between our Get and Claim: wait, then loop.
			if time.Until(deadline) <= 0 {
				return nil, ErrConflict
			}
			if _, werr := b.waitForCompletion(ctx, cacheKey, deadline); werr != nil {
				return nil, werr
			}
			continue
		default:
			// Claim converted a terminal row to building for us but reported
			// not-claimed unexpectedly; treat as needing another iteration.
			continue
		}
	}
}

// BadRequestError marks user-input failures (HTTP 400).
type BadRequestError struct{ Msg string }

func (e *BadRequestError) Error() string { return e.Msg }

func hitOutcome(cacheKey string, e *store.Entry, m *key.Material, raw []byte, qs []store.QuarantineEvent) *Outcome {
	return &Outcome{
		Key: cacheKey, Hit: true, Status: e.Status, Attempt: e.Attempt,
		ArtifactSHA: e.ArtifactSHA, ArtifactSize: e.ArtifactSize,
		Quarantined: qs, KeyMaterial: m, CanonicalMaterialJSON: raw,
	}
}

// runAsPublisher executes the real compilation and publishes exactly one
// terminal status. Failure paths always publish "failed", never "succeeded".
func (b *Builder) runAsPublisher(ctx context.Context, req *Request, m *key.Material,
	rawJSON []byte, cacheKey string, e *store.Entry) *Outcome {
	start := time.Now()
	out := &Outcome{Key: cacheKey, Hit: false, Attempt: e.Attempt,
		KeyMaterial: m, CanonicalMaterialJSON: rawJSON}

	timeout := 120 * time.Second
	if req.TimeoutSeconds > 0 {
		timeout = time.Duration(req.TimeoutSeconds) * time.Second
	}
	cctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	workDir, err := os.MkdirTemp("", "buildcache-work-")
	if err != nil {
		b.publishFailure(ctx, cacheKey, e, -1, "", "", "workdir: "+err.Error())
		out.Status, out.Error = store.StatusFailed, err.Error()
		return out
	}
	defer os.RemoveAll(workDir)

	if err := b.materializeSources(workDir, req.Sources, m.Sources); err != nil {
		b.publishFailure(ctx, cacheKey, e, -1, "", "", "materialize: "+err.Error())
		out.Status, out.Error = store.StatusFailed, err.Error()
		return out
	}

	argv := expandTemplate(req.CommandTemplate, req.Args, req.Artifact, m.Sources)
	cmd := exec.CommandContext(cctx, argv[0], argv[1:]...)
	cmd.Dir = workDir
	cmd.Env = childEnv(req.Target, req.Env)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	runErr := cmd.Run()

	exitCode := 0
	if runErr != nil {
		if ee, ok := runErr.(*exec.ExitError); ok {
			exitCode = ee.ExitCode()
		} else {
			// Process never ran / timed out: real failure, recorded as such.
			msg := "execute: " + runErr.Error()
			b.publishFailure(ctx, cacheKey, e, -1, stdout.String(), stderr.String(), msg)
			out.Status, out.ExitCode, out.Stdout, out.Stderr, out.Error =
				store.StatusFailed, -1, stdout.String(), stderr.String(), msg
			out.ElapsedMS = time.Since(start).Milliseconds()
			return out
		}
	}

	if exitCode != 0 {
		msg := fmt.Sprintf("toolchain exited with code %d", exitCode)
		b.publishFailure(ctx, cacheKey, e, exitCode, stdout.String(), stderr.String(), msg)
		out.Status, out.ExitCode, out.Stdout, out.Stderr, out.Error =
			store.StatusFailed, exitCode, stdout.String(), stderr.String(), msg
		out.ElapsedMS = time.Since(start).Milliseconds()
		return out
	}

	// Exit code 0 still does not imply success: verify the declared artifact
	// actually exists. A "successful" run with no output is a failure and is
	// never cached as a success.
	artPath := filepath.Join(workDir, filepath.Clean("/"+req.Artifact))
	sum, size, err := hashFile(artPath)
	if err != nil {
		msg := "artifact not produced: " + err.Error()
		b.publishFailure(ctx, cacheKey, e, 0, stdout.String(), stderr.String(), msg)
		out.Status, out.ExitCode, out.Stdout, out.Stderr, out.Error =
			store.StatusFailed, 0, stdout.String(), stderr.String(), msg
		out.ElapsedMS = time.Since(start).Milliseconds()
		return out
	}

	// Content-addressable storage: blob name is its digest.
	rel := sum[:2] + "/" + sum
	dst := filepath.Join(b.blobsDir, rel)
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		b.publishFailure(ctx, cacheKey, e, 0, stdout.String(), stderr.String(), "store mkdir: "+err.Error())
		out.Status, out.Error = store.StatusFailed, err.Error()
		return out
	}
	if err := moveOrCopy(artPath, dst); err != nil {
		b.publishFailure(ctx, cacheKey, e, 0, stdout.String(), stderr.String(), "store artifact: "+err.Error())
		out.Status, out.Error = store.StatusFailed, err.Error()
		return out
	}
	// Re-read from the store to prove the bytes landed intact.
	verifySum, verifySize, err := hashFile(dst)
	if err != nil || verifySum != sum || verifySize != size {
		_ = os.Remove(dst)
		msg := "post-store verification failed"
		b.publishFailure(ctx, cacheKey, e, 0, stdout.String(), stderr.String(), msg)
		out.Status, out.Error = store.StatusFailed, msg
		return out
	}

	e.Status = store.StatusSucceeded
	e.ExitCode = 0
	e.Stdout, e.Stderr = stdout.String(), stderr.String()
	e.ErrMessage = ""
	e.ArtifactPath, e.ArtifactSize, e.ArtifactSHA = rel, size, sum
	if perr := b.st.PublishResult(ctx, e); perr != nil {
		// Cannot durably record success: report failure honestly rather than
		// claiming a cached artifact that the DB does not know about.
		_ = os.Remove(dst)
		out.Status, out.Error = store.StatusFailed, "publish: "+perr.Error()
		return out
	}

	out.Status = store.StatusSucceeded
	out.ArtifactSHA, out.ArtifactSize = sum, size
	out.Stdout, out.Stderr = stdout.String(), stderr.String()
	out.ElapsedMS = time.Since(start).Milliseconds()
	return out
}

func (b *Builder) publishFailure(ctx context.Context, cacheKey string, e *store.Entry,
	exitCode int, stdout, stderr, msg string) {
	e.Status = store.StatusFailed
	e.ExitCode = exitCode
	e.Stdout, e.Stderr, e.ErrMessage = stdout, stderr, msg
	_ = b.st.PublishResult(ctx, e)
}

// expandTemplate replaces the {sources} token with the sorted manifest paths,
// {artifact} with the declared output path, and {args} with the argument
// vector. Tokens never pass through a shell: every element stays a distinct
// argv entry.
func expandTemplate(tmpl, args []string, artifact string, sources []key.SourceEntry) []string {
	paths := make([]string, 0, len(sources))
	for _, s := range sources {
		paths = append(paths, s.Path)
	}
	sort.Strings(paths)

	artifactName := filepath.Clean("/" + artifact)
	artifactName = strings.TrimPrefix(artifactName, "/")

	argv := make([]string, 0, len(tmpl)+len(args))
	for _, tok := range tmpl {
		switch tok {
		case "{sources}":
			argv = append(argv, paths...)
		case "{args}":
			argv = append(argv, args...)
		case "{artifact}":
			argv = append(argv, artifactName)
		default:
			argv = append(argv, tok)
		}
	}
	return argv
}

// moveOrCopy renames when possible (same filesystem), otherwise copies.
func moveOrCopy(src, dst string) error {
	if err := os.Rename(src, dst); err == nil {
		return nil
	}
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	if err := out.Chmod(0o755); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// ArtifactReader opens a verified artifact for download and re-verifies its
// digest before returning bytes.
func (b *Builder) ArtifactReader(ctx context.Context, cacheKey string) (io.ReadCloser, *store.Entry, *store.QuarantineEvent, error) {
	e, err := b.st.Get(ctx, cacheKey)
	if err != nil {
		return nil, nil, nil, err
	}
	if e.Status != store.StatusSucceeded {
		// A failed / building / quarantined / interrupted entry has no
		// artifact to verify or serve; surface its status directly.
		return nil, e, nil, nil
	}
	q, err := b.ensureArtifact(ctx, e)
	if err != nil {
		return nil, nil, nil, err
	}
	if q != nil {
		// Return a fresh row reflecting the quarantine, not the pre-check snapshot.
		fresh, gerr := b.st.Get(ctx, cacheKey)
		if gerr == nil {
			e = fresh
		}
		return nil, e, q, nil
	}
	f, err := os.Open(b.BlobPath(e.ArtifactPath))
	if err != nil {
		return nil, nil, nil, err
	}
	return f, e, nil, nil
}

// Inspect returns the stored entry (converting ErrNotFound to nil).
func (b *Builder) Inspect(ctx context.Context, cacheKey string) (*store.Entry, error) {
	e, err := b.st.Get(ctx, cacheKey)
	if errors.Is(err, store.ErrNotFound) {
		return nil, nil
	}
	return e, err
}

// Events lists quarantine events for a key.
func (b *Builder) Events(ctx context.Context, cacheKey string) ([]store.QuarantineEvent, error) {
	return b.st.QuarantineEvents(ctx, cacheKey)
}
