// Package key computes the audit-grade cache key for a build.
//
// The key binds together:
//   - the source manifest (paths sorted, content hashed; missing vs empty
//     files distinguished),
//   - the toolchain summary (command + version output + binary digest),
//   - the argument vector,
//   - the target platform,
//   - the declared environment variables (sorted name=value pairs).
//
// Material is serialized in one canonical, versioned JSON document and hashed
// with SHA-256. All computation here is real and deterministic: there is no
// hostname, clock value, random nonce, or ambient environment in the key.
package key

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"path"
	"sort"
	"strings"
)

// KeyVersion is the prefix embedded in the key material envelope. Any change
// to the canonicalization or binding rules bumps this, so old entries can
// never be interpreted under new rules.
const KeyVersion = "bck1"

// SourceEntry is one file in the source manifest.
type SourceEntry struct {
	// Path is the forward-slash path of the file relative to the source root.
	Path string `json:"path"`
	// Present reports whether the file existed. A present, zero-byte file
	// has Present=true and empty digest; an absent file has Present=false.
	Present bool `json:"present"`
	// Digest is the hex SHA-256 of the file bytes ("" when absent).
	Digest string `json:"digest"`
	// Size is the file size in bytes.
	Size int64 `json:"size"`
}

// ToolchainSummary describes the compiler used for the build.
type ToolchainSummary struct {
	// Name is the logical toolchain name, e.g. "go".
	Name string `json:"name"`
	// VersionCommand is the argv executed to obtain VersionOutput, e.g.
	// ["go","version"].
	VersionCommand []string `json:"version_command"`
	// VersionOutput is the captured stdout of VersionCommand, trimmed.
	VersionOutput string `json:"version_output"`
	// BinaryPath is the resolved executable that actually ran.
	BinaryPath string `json:"binary_path"`
	// BinaryDigest is the hex SHA-256 of that executable (or "" if it could
	// not be read, e.g. a shell wrapper).
	BinaryDigest string `json:"binary_digest"`
}

// EnvVar is one declared environment variable. Only variables the client
// declares participate in the key; ambient server variables never do.
type EnvVar struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

// Material is the complete, keyable description of a build.
type Material struct {
	Toolchain ToolchainSummary  `json:"toolchain"`
	Args      []string          `json:"args"`
	Command   []string          `json:"command"`
	Target    map[string]string `json:"target"`
	Env       []EnvVar          `json:"env"`
	Sources   []SourceEntry     `json:"sources"`
}

// materialDoc is the versioned envelope that is actually marshaled. Field
// order here is the canonical field order.
type materialDoc struct {
	Version   string           `json:"version"`
	Toolchain ToolchainSummary `json:"toolchain"`
	Args      []string         `json:"args"`
	Command   []string         `json:"command"`
	Target    [][2]string      `json:"target"`
	Env       []EnvVar         `json:"env"`
	Sources   []SourceEntry    `json:"sources"`
}

// NormalizePath cleans a client-supplied relative path: backslashes are
// treated as separators, the result is slash-separated, rooted, cleaned, and
// de-rooted. It rejects absolute paths and traversal escaping the root.
func NormalizePath(p string) (string, error) {
	if p == "" {
		return "", fmt.Errorf("empty source path")
	}
	s := strings.ReplaceAll(p, "\\", "/")
	if strings.HasPrefix(s, "/") {
		return "", fmt.Errorf("source path must be relative: %q", p)
	}
	// A relative clean (without clamping to a root) reveals inputs that
	// try to walk above the source root; reject them outright instead of
	// silently remapping them inside the root.
	probe := path.Clean(s)
	if probe == ".." || strings.HasPrefix(probe, "../") {
		return "", fmt.Errorf("source path escapes root: %q", p)
	}
	c := path.Clean("/" + s)
	if c == "/" {
		return "", fmt.Errorf("source path resolves to root: %q", p)
	}
	if strings.HasSuffix(s, "/") {
		return "", fmt.Errorf("source path must be a file, got directory: %q", p)
	}
	return strings.TrimPrefix(c, "/"), nil
}

// Canonicalize validates the material, sorts the parts whose order must not
// matter (sources by path, env by name, target by key), and returns the
// canonical JSON document bytes plus the cache key.
//
// Slices that are logically sets are sorted; Args and Command keep their
// submitted order because argument order can be semantically meaningful.
func Canonicalize(m *Material) ([]byte, string, error) {
	if m == nil {
		return nil, "", fmt.Errorf("nil key material")
	}
	if strings.TrimSpace(m.Toolchain.Name) == "" {
		return nil, "", fmt.Errorf("toolchain.name is required")
	}
	if len(m.Toolchain.VersionCommand) == 0 {
		return nil, "", fmt.Errorf("toolchain.version_command is required")
	}

	// Sources: validate, normalize, sort by path. Duplicate paths rejected.
	srcs := make([]SourceEntry, len(m.Sources))
	seen := make(map[string]struct{}, len(m.Sources))
	for i, s := range m.Sources {
		np, err := NormalizePath(s.Path)
		if err != nil {
			return nil, "", err
		}
		if _, dup := seen[np]; dup {
			return nil, "", fmt.Errorf("duplicate source path: %q", np)
		}
		seen[np] = struct{}{}
		if s.Present {
			if len(s.Digest) != 64 {
				return nil, "", fmt.Errorf("source %q: present file requires 64-hex digest", np)
			}
		} else {
			if s.Digest != "" || s.Size != 0 {
				return nil, "", fmt.Errorf("source %q: absent file must have empty digest and zero size", np)
			}
		}
		srcs[i] = SourceEntry{Path: np, Present: s.Present, Digest: strings.ToLower(s.Digest), Size: s.Size}
	}
	sort.Slice(srcs, func(i, j int) bool { return srcs[i].Path < srcs[j].Path })

	// Env: validate names, sort by name.
	envs := make([]EnvVar, len(m.Env))
	envSeen := make(map[string]struct{}, len(m.Env))
	for i, e := range m.Env {
		if strings.TrimSpace(e.Name) == "" || strings.ContainsAny(e.Name, "=") || strings.Contains(e.Name, "\x00") {
			return nil, "", fmt.Errorf("invalid env var name: %q", e.Name)
		}
		if _, dup := envSeen[e.Name]; dup {
			return nil, "", fmt.Errorf("duplicate env var: %q", e.Name)
		}
		envSeen[e.Name] = struct{}{}
		envs[i] = EnvVar{Name: e.Name, Value: e.Value}
	}
	sort.Slice(envs, func(i, j int) bool { return envs[i].Name < envs[j].Name })

	// Target: canonicalize to sorted key/value pairs.
	target := make([][2]string, 0, len(m.Target))
	for k, v := range m.Target {
		if strings.TrimSpace(k) == "" {
			return nil, "", fmt.Errorf("target has empty key")
		}
		target = append(target, [2]string{k, v})
	}
	sort.Slice(target, func(i, j int) bool { return target[i][0] < target[j][0] })

	args := m.Args
	if args == nil {
		args = []string{}
	}
	cmd := m.Command
	if cmd == nil {
		cmd = []string{}
	}

	doc := materialDoc{
		Version:   KeyVersion,
		Toolchain: m.Toolchain,
		Args:      args,
		Command:   cmd,
		Target:    target,
		Env:       envs,
		Sources:   srcs,
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		return nil, "", fmt.Errorf("canonical marshal: %w", err)
	}
	sum := sha256.Sum256(raw)
	return raw, KeyVersion + "-" + hex.EncodeToString(sum[:]), nil
}
