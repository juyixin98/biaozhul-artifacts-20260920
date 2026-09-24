// Package builder really executes a build command in a task directory
// and smoke-runs the produced artifact. The subprocess receives only a
// minimal environment plus the caller's declared variables — undeclared
// environment cannot leak into the build.
package builder

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"sort"
	"strings"
	"time"
)

type Request struct {
	TaskDir string            // absolute path; subprocess working directory
	Sources []string          // declared source paths (for $SOURCES)
	Command string            // run via /bin/sh -c; must write the artifact to $OUT
	Env     map[string]string // declared environment, bound into the cache key
}

var envNameRe = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

const maxOutput = 64 * 1024

// Run executes the build, expecting it to produce outPath, then runs the
// artifact with no arguments as a smoke test and returns its output.
// Any failure — compile error, missing artifact, failing smoke run — is
// returned as an error; nothing is cached by this package either way.
func Run(ctx context.Context, req Request, outPath string) (string, error) {
	env := []string{
		"PATH=" + os.Getenv("PATH"),
		"TMPDIR=" + os.TempDir(),
		"OUT=" + outPath,
		"SOURCES=" + shellJoin(req.Sources),
	}
	names := make([]string, 0, len(req.Env))
	for k := range req.Env {
		if !envNameRe.MatchString(k) {
			return "", fmt.Errorf("invalid environment variable name %q", k)
		}
		names = append(names, k)
	}
	sort.Strings(names)
	for _, k := range names {
		env = append(env, k+"="+req.Env[k])
	}

	bctx, cancel := context.WithTimeout(ctx, 120*time.Second)
	defer cancel()
	cmd := exec.CommandContext(bctx, "/bin/sh", "-c", req.Command)
	cmd.Dir = req.TaskDir
	cmd.Env = env
	var buildOut bytes.Buffer
	cmd.Stdout = &cappedWriter{w: &buildOut}
	cmd.Stderr = &cappedWriter{w: &buildOut}
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("build command failed: %v\n%s", err, buildOut.String())
	}
	if _, err := os.Stat(outPath); err != nil {
		return "", fmt.Errorf("build succeeded but did not produce $OUT: %v", err)
	}

	// Smoke-run the artifact: proof that the cached entry is real.
	rctx, rcancel := context.WithTimeout(ctx, 15*time.Second)
	defer rcancel()
	run := exec.CommandContext(rctx, outPath)
	run.Dir = req.TaskDir
	run.Env = env
	var runOut bytes.Buffer
	run.Stdout = &cappedWriter{w: &runOut}
	run.Stderr = &cappedWriter{w: &runOut}
	if err := run.Run(); err != nil {
		return "", fmt.Errorf("artifact smoke run failed: %v\n%s", err, runOut.String())
	}
	return runOut.String(), nil
}

func shellJoin(paths []string) string {
	var b strings.Builder
	for i, p := range paths {
		if i > 0 {
			b.WriteByte(' ')
		}
		b.WriteByte('\'')
		b.WriteString(strings.ReplaceAll(p, "'", `'\''`))
		b.WriteByte('\'')
	}
	return b.String()
}

// cappedWriter drops bytes beyond maxOutput so a chatty build cannot
// exhaust memory.
type cappedWriter struct {
	w *bytes.Buffer
}

func (c *cappedWriter) Write(p []byte) (int, error) {
	remaining := maxOutput - c.w.Len()
	if remaining > 0 {
		if len(p) > remaining {
			c.w.Write(p[:remaining])
		} else {
			c.w.Write(p)
		}
	}
	return len(p), nil
}
