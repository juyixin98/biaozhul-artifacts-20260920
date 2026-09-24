// Package cachekey computes deterministic build cache keys.
//
// A cache key binds together every input that may influence a build:
//   - the source manifest (paths sorted in byte order; a missing file is
//     recorded distinctly from an empty file),
//   - the toolchain digest (SHA-256 of the compiler binary and of its
//     --version output),
//   - the build command,
//   - the target platform,
//   - the declared environment variables.
//
// The canonical encoding length-prefixes every field, so no two different
// input sets can ever serialize to the same byte string.
package cachekey

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// KeyVersion versions the canonical encoding; bump on any format change.
const KeyVersion = "buildcache-key/v1"

// SourceState distinguishes a file that exists (possibly empty) from one
// that does not exist at all.
type SourceState string

const (
	StatePresent SourceState = "present"
	StateMissing SourceState = "missing"
)

// SourceEntry is one row of the source manifest.
type SourceEntry struct {
	Path   string      `json:"path"`
	State  SourceState `json:"state"`
	Size   int64       `json:"size"`
	SHA256 string      `json:"sha256,omitempty"` // empty when State == StateMissing
}

// Toolchain captures the resolved compiler identity.
type Toolchain struct {
	Name          string `json:"name"`
	ResolvedPath  string `json:"resolved_path"`
	BinarySHA256  string `json:"binary_sha256"`
	VersionSHA256 string `json:"version_sha256"`
	VersionLine   string `json:"version_line"`
}

// Inputs is the complete set of values bound into a cache key.
type Inputs struct {
	Platform  string            `json:"platform"`
	Command   string            `json:"command"`
	Env       map[string]string `json:"env"`
	Toolchain Toolchain         `json:"toolchain"`
	Sources   []SourceEntry     `json:"sources"`
}

// lp appends one length-prefixed field: "<byteLen>:<bytes>|".
func lp(b *strings.Builder, s string) {
	fmt.Fprintf(b, "%d:%s|", len(s), s)
}

// Canonical returns the canonical serialization of the inputs.
func (in Inputs) Canonical() string {
	var b strings.Builder
	lp(&b, KeyVersion)
	lp(&b, "platform")
	lp(&b, in.Platform)
	lp(&b, "command")
	lp(&b, in.Command)
	lp(&b, "toolchain")
	lp(&b, in.Toolchain.Name)
	lp(&b, in.Toolchain.BinarySHA256)
	lp(&b, in.Toolchain.VersionSHA256)
	lp(&b, "env")
	envKeys := make([]string, 0, len(in.Env))
	for k := range in.Env {
		envKeys = append(envKeys, k)
	}
	sort.Strings(envKeys)
	for _, k := range envKeys {
		lp(&b, k)
		lp(&b, in.Env[k])
	}
	lp(&b, "/env")
	lp(&b, "sources")
	for _, s := range in.Sources {
		lp(&b, s.Path)
		lp(&b, string(s.State))
		lp(&b, strconv.FormatInt(s.Size, 10))
		lp(&b, s.SHA256)
	}
	lp(&b, "/sources")
	return b.String()
}

// Key returns the hex SHA-256 of the canonical serialization.
func (in Inputs) Key() string {
	sum := sha256.Sum256([]byte(in.Canonical()))
	return hex.EncodeToString(sum[:])
}

// ValidateRelPath cleans p and rejects anything that escapes the task dir.
func ValidateRelPath(p string) (string, error) {
	if p == "" {
		return "", fmt.Errorf("empty path")
	}
	if filepath.IsAbs(p) {
		return "", fmt.Errorf("path %q is absolute", p)
	}
	c := filepath.Clean(p)
	if c == ".." || strings.HasPrefix(c, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("path %q escapes the task directory", p)
	}
	return c, nil
}

// BuildManifest reads the given source paths under root in deterministic
// (byte-sorted) order and records each file's state, size and SHA-256.
// A nonexistent path yields a StateMissing entry (distinct from an empty
// file, which is StatePresent with the well-known empty SHA-256). Paths
// are de-duplicated; duplicates in the request do not change the key.
func BuildManifest(root string, paths []string) ([]SourceEntry, error) {
	seen := map[string]bool{}
	clean := make([]string, 0, len(paths))
	for _, p := range paths {
		c, err := ValidateRelPath(p)
		if err != nil {
			return nil, err
		}
		if !seen[c] {
			seen[c] = true
			clean = append(clean, c)
		}
	}
	sort.Strings(clean)

	entries := make([]SourceEntry, 0, len(clean))
	for _, p := range clean {
		full := filepath.Join(root, p)
		fi, err := os.Lstat(full)
		switch {
		case os.IsNotExist(err):
			entries = append(entries, SourceEntry{Path: p, State: StateMissing, Size: -1})
		case err != nil:
			return nil, fmt.Errorf("stat %s: %w", p, err)
		case !fi.Mode().IsRegular():
			return nil, fmt.Errorf("source %s is not a regular file", p)
		default:
			h := sha256.New()
			f, err := os.Open(full)
			if err != nil {
				return nil, fmt.Errorf("open %s: %w", p, err)
			}
			n, err := io.Copy(h, f)
			f.Close()
			if err != nil {
				return nil, fmt.Errorf("hash %s: %w", p, err)
			}
			entries = append(entries, SourceEntry{
				Path:   p,
				State:  StatePresent,
				Size:   n,
				SHA256: hex.EncodeToString(h.Sum(nil)),
			})
		}
	}
	return entries, nil
}
