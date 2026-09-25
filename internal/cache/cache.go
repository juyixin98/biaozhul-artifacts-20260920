// Package cache implements a content-addressed build cache that lives
// outside the workspace.
//
// Layout (all paths under Root):
//
//	entries/<aa>/<key>/meta.json    entry metadata and manifest
//	entries/<aa>/<key>/out/<rel>   captured output files
//	history/<slug>.json            per-project last-known fingerprints
//
// A cache entry is keyed by the node fingerprint hash and is shared
// across projects: identical inputs, tools, params and env reproduce
// identical outputs, so a store built for one workspace can restore a
// node in another. History is project-local because it records the last
// fingerprint used in *this* workspace, which is what lets the engine
// explain why a node rebuilt.
//
// Entries are written atomically (temp directory + rename) so a failed
// or interrupted node never publishes a partial entry.
package cache

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"

	"cdag/internal/fingerprint"
)

// EntryMeta is the manifest stored alongside a cached node's outputs.
type EntryMeta struct {
	Version     int                     `json:"version"`
	Key         string                  `json:"key"`
	NodeID      string                  `json:"node_id"`
	Outputs     []fingerprint.FileRef   `json:"outputs"`
	Fingerprint fingerprint.Fingerprint `json:"fingerprint"`
}

// Cache is a content-addressed store plus per-project history.
type Cache struct {
	root string
}

// Open creates or opens a cache rooted at root.
func Open(root string) (*Cache, error) {
	if root == "" {
		return nil, errors.New("cache root is empty")
	}
	for _, sub := range []string{"entries", "history"} {
		if err := os.MkdirAll(filepath.Join(root, sub), 0o755); err != nil {
			return nil, fmt.Errorf("create cache dir: %w", err)
		}
	}
	return &Cache{root: root}, nil
}

// Root returns the cache root directory.
func (c *Cache) Root() string { return c.root }

func (c *Cache) entryDir(key string) string {
	return filepath.Join(c.root, "entries", key[:2], key)
}

// Has reports whether an entry for key exists.
func (c *Cache) Has(key string) bool {
	if len(key) < 2 {
		return false
	}
	_, err := os.Stat(filepath.Join(c.entryDir(key), "meta.json"))
	return err == nil
}

// Lookup returns the metadata of a cached entry.
func (c *Cache) Lookup(key string) (*EntryMeta, error) {
	if len(key) < 2 {
		return nil, fmt.Errorf("invalid cache key %q", key)
	}
	data, err := os.ReadFile(filepath.Join(c.entryDir(key), "meta.json"))
	if err != nil {
		return nil, err
	}
	var meta EntryMeta
	if err := json.Unmarshal(data, &meta); err != nil {
		return nil, fmt.Errorf("parse entry meta: %w", err)
	}
	return &meta, nil
}

// WriteEntry atomically captures output files from workdir and stores
// them under key. It must only be called after a node command succeeded
// and all declared outputs exist; a failed node never calls this.
func (c *Cache) WriteEntry(key, nodeID string, workdir string, outs []fingerprint.FileRef, fp fingerprint.Fingerprint) error {
	if len(key) < 2 {
		return fmt.Errorf("invalid cache key %q", key)
	}
	final := c.entryDir(key)
	if _, err := os.Stat(filepath.Join(final, "meta.json")); err == nil {
		return nil // already published (deterministic; concurrent rebuilds converge)
	}
	tmp, err := os.MkdirTemp(filepath.Join(c.root, "entries"), ".tmp-*")
	if err != nil {
		return err
	}
	cleanup := true
	defer func() {
		if cleanup {
			_ = os.RemoveAll(tmp)
		}
	}()

	outRoot := filepath.Join(tmp, "out")
	manifest := make([]fingerprint.FileRef, 0, len(outs))
	for _, ref := range outs {
		src := filepath.Join(workdir, filepath.FromSlash(ref.Path))
		data, err := os.ReadFile(src)
		if err != nil {
			return fmt.Errorf("stage output %s: %w", ref.Path, err)
		}
		dst := filepath.Join(outRoot, filepath.FromSlash(ref.Path))
		if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
			return err
		}
		mode := os.FileMode(ref.Mode)
		if mode == 0 {
			mode = 0o644
		}
		if err := os.WriteFile(dst, data, mode); err != nil {
			return err
		}
		// Verify content identity on the way in.
		reRef, err := fingerprint.HashFile(tmp, filepath.Join("out", ref.Path))
		if err != nil {
			return err
		}
		if reRef.SHA != ref.SHA {
			return fmt.Errorf("output %s content changed during caching", ref.Path)
		}
		manifest = append(manifest, ref)
	}
	sort.Slice(manifest, func(i, j int) bool { return manifest[i].Path < manifest[j].Path })

	meta := EntryMeta{Version: 1, Key: key, NodeID: nodeID, Outputs: manifest, Fingerprint: fp}
	mdata, err := json.MarshalIndent(meta, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(tmp, "meta.json"), mdata, 0o644); err != nil {
		return err
	}

	if err := os.MkdirAll(filepath.Dir(final), 0o755); err != nil {
		return err
	}
	if err := os.Rename(tmp, final); err != nil {
		if errors.Is(err, os.ErrExist) || c.Has(key) {
			// Another writer won the race; its content is identical by key.
			return nil
		}
		return err
	}
	cleanup = false
	return nil
}

// Restore copies a cached entry's output files back into workdir,
// creating parent directories as needed.
func (c *Cache) Restore(key, workdir string) (*EntryMeta, error) {
	meta, err := c.Lookup(key)
	if err != nil {
		return nil, err
	}
	srcRoot := filepath.Join(c.entryDir(key), "out")
	for _, ref := range meta.Outputs {
		src := filepath.Join(srcRoot, filepath.FromSlash(ref.Path))
		data, err := os.ReadFile(src)
		if err != nil {
			return nil, fmt.Errorf("read cached output %s: %w", ref.Path, err)
		}
		dst := filepath.Join(workdir, filepath.FromSlash(ref.Path))
		if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
			return nil, err
		}
		mode := os.FileMode(ref.Mode)
		if mode == 0 {
			mode = 0o644
		}
		if err := os.WriteFile(dst, data, mode); err != nil {
			return nil, fmt.Errorf("restore output %s: %w", ref.Path, err)
		}
	}
	return meta, nil
}
