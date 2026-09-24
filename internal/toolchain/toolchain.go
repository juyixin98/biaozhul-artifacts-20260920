// Package toolchain resolves a compiler to a verifiable identity: the
// SHA-256 of the binary on disk plus the SHA-256 of its --version output.
// Both are really executed/measured, never assumed.
package toolchain

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"buildcache/internal/cachekey"
)

// Resolve locates name (a bare command looked up in PATH, or a path
// relative to taskDir), hashes the binary and runs it with --version.
func Resolve(ctx context.Context, name, taskDir string) (cachekey.Toolchain, error) {
	tc := cachekey.Toolchain{Name: name}
	if name == "" {
		return tc, fmt.Errorf("empty toolchain name")
	}

	var path string
	if strings.ContainsRune(name, '/') {
		p := name
		if !filepath.IsAbs(p) {
			p = filepath.Join(taskDir, p)
		}
		abs, err := filepath.Abs(p)
		if err != nil {
			return tc, err
		}
		if _, err := os.Stat(abs); err != nil {
			return tc, fmt.Errorf("tool %q: %w", name, err)
		}
		path = abs
	} else {
		p, err := exec.LookPath(name)
		if err != nil {
			return tc, fmt.Errorf("tool %q not found in PATH: %w", name, err)
		}
		path = p
	}
	tc.ResolvedPath = path

	h := sha256.New()
	f, err := os.Open(path)
	if err != nil {
		return tc, fmt.Errorf("open tool %s: %w", path, err)
	}
	if _, err := io.Copy(h, f); err != nil {
		f.Close()
		return tc, fmt.Errorf("hash tool %s: %w", path, err)
	}
	f.Close()
	tc.BinarySHA256 = hex.EncodeToString(h.Sum(nil))

	// Probe the version. Some tools have no --version; record honestly
	// whatever happens (including the error text) so the key still
	// reflects reality.
	vctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	out, err := exec.CommandContext(vctx, path, "--version").CombinedOutput()
	if len(out) > 4096 {
		out = out[:4096]
	}
	if err != nil {
		out = append(out, []byte("\n<probe error: "+err.Error()+">")...)
	}
	vsum := sha256.Sum256(out)
	tc.VersionSHA256 = hex.EncodeToString(vsum[:])
	line, _, _ := strings.Cut(string(out), "\n")
	tc.VersionLine = strings.TrimSpace(line)
	return tc, nil
}

// Platform returns a string identifying the target platform, e.g.
// "linux/amd64 Linux 6.8.0-90-generic x86_64".
func Platform() string {
	out, err := exec.Command("uname", "-srm").Output()
	if err != nil {
		return runtime.GOOS + "/" + runtime.GOARCH
	}
	return runtime.GOOS + "/" + runtime.GOARCH + " " + strings.TrimSpace(string(out))
}
