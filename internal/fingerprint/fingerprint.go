// Package fingerprint computes the content-derived cache key for a node.
//
// A fingerprint is a SHA-256 over the canonical JSON encoding of:
//
//   - node id
//   - each declared input file's relative path, mode bits and content hash
//   - each dependency's node id and resulting fingerprint
//   - each dependency output file's relative path, mode bits and content hash
//   - sorted params
//   - sorted declared environment variables with their captured values
//   - each declared tool's name and probed version
//
// File modification timestamps are deliberately NOT part of the key: the
// build is content-driven, so touching a file without changing its bytes
// must not invalidate a cache entry.
package fingerprint

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"

	"cdag/internal/graph"
)

// FileRef is a workspace file captured by its content.
type FileRef struct {
	Path string `json:"path"`
	Mode uint32 `json:"mode"`
	SHA  string `json:"sha256"`
}

// DepRef captures everything a node inherits from one dependency edge:
// the dependency's own fingerprint (propagating parameter/tool/env changes)
// plus the byte content of its output files (propagating content changes).
type DepRef struct {
	ID          string    `json:"id"`
	Fingerprint string    `json:"fingerprint"`
	Outputs     []FileRef `json:"outputs,omitempty"`
}

// Fingerprint is the complete content-derived identity of a node build.
type Fingerprint struct {
	NodeID string        `json:"node_id"`
	Inputs []FileRef     `json:"inputs,omitempty"`
	Deps   []DepRef      `json:"deps,omitempty"`
	Params []KV          `json:"params,omitempty"`
	Env    []KV          `json:"env,omitempty"`
	Tools  []ToolVersion `json:"tools,omitempty"`
}

// KV is a sorted key/value pair used for params and env.
type KV struct {
	Key string `json:"key"`
	Val string `json:"val"`
}

// ToolVersion is a declared tool and the version string probed for it.
type ToolVersion struct {
	Name    string `json:"name"`
	Version string `json:"version"`
}

// HashFile reads a regular file and returns a content reference.
func HashFile(workdir, rel string) (FileRef, error) {
	full := filepath.Join(workdir, filepath.FromSlash(rel))
	data, err := os.ReadFile(full)
	if err != nil {
		return FileRef{}, err
	}
	sum := sha256.Sum256(data)
	var mode uint32 = 0o644
	if fi, err := os.Stat(full); err == nil {
		mode = uint32(fi.Mode().Perm())
	}
	return FileRef{
		Path: rel,
		Mode: mode,
		SHA:  hex.EncodeToString(sum[:]),
	}, nil
}

// HashFiles hashes a list of relative paths and returns references sorted
// by path. A missing or unreadable file returns an error.
func HashFiles(workdir string, rels []string) ([]FileRef, error) {
	refs := make([]FileRef, 0, len(rels))
	seen := map[string]bool{}
	for _, rel := range rels {
		if seen[rel] {
			continue
		}
		seen[rel] = true
		ref, err := HashFile(workdir, rel)
		if err != nil {
			return nil, err
		}
		refs = append(refs, ref)
	}
	sort.Slice(refs, func(i, j int) bool { return refs[i].Path < refs[j].Path })
	return refs, nil
}

// Input assembles a Fingerprint value from the node declaration and its
// resolved inputs. It performs no hashing itself beyond Key(); callers
// supply dependency references and tool versions resolved earlier.
func Input(n *graph.Node, inputs []FileRef, deps []DepRef, tools []ToolVersion, envValues map[string]string) Fingerprint {
	fp := Fingerprint{
		NodeID: n.ID,
		Inputs: inputs,
		Deps:   deps,
		Tools:  tools,
	}
	for k, v := range n.Params {
		fp.Params = append(fp.Params, KV{Key: k, Val: v})
	}
	sort.Slice(fp.Params, func(i, j int) bool { return fp.Params[i].Key < fp.Params[j].Key })
	for _, name := range n.Env {
		fp.Env = append(fp.Env, KV{Key: name, Val: envValues[name]})
	}
	sort.Slice(fp.Env, func(i, j int) bool { return fp.Env[i].Key < fp.Env[j].Key })
	sort.Slice(fp.Deps, func(i, j int) bool { return fp.Deps[i].ID < fp.Deps[j].ID })
	sort.Slice(fp.Tools, func(i, j int) bool { return fp.Tools[i].Name < fp.Tools[j].Name })
	return fp
}

// Key returns the hex SHA-256 of the canonical encoding of the fingerprint.
func (f Fingerprint) Key() (string, error) {
	// Deterministic encoding: all slices are pre-sorted by Input() and
	// json.Marshal of structs follows field declaration order.
	raw, err := json.Marshal(f)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

// CanonicalJSON returns the deterministically encoded fingerprint bytes,
// used for human inspection and tests.
func (f Fingerprint) CanonicalJSON() ([]byte, error) {
	return json.MarshalIndent(f, "", "  ")
}
