// Package cache implements a tiny on-disk solve-result cache. The cache
// directory always lives outside the working directory: it uses
// os.UserCacheDir() (e.g. ~/.cache/depresolve) and falls back to the
// system temp dir. Nothing is ever downloaded; the cache only memoizes
// results of pure local computations.
package cache

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// Cache stores JSON blobs keyed by the SHA-256 of the canonical request.
type Cache struct {
	dir string
}

// DefaultDir returns the cache directory, which is guaranteed to be
// outside the process working directory.
func DefaultDir() (string, error) {
	base, err := os.UserCacheDir()
	if err != nil || base == "" {
		base = os.TempDir()
	}
	return filepath.Join(base, "depresolve"), nil
}

// Open creates (if needed) a cache under dir.
func Open(dir string) (*Cache, error) {
	if dir == "" {
		return nil, errors.New("cache: empty directory")
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("cache: %w", err)
	}
	return &Cache{dir: dir}, nil
}

// Key derives the cache key from any JSON-marshable request. Go's
// encoding/json emits map keys in sorted order, so the encoding is
// canonical for our request types.
func Key(req any) (string, error) {
	b, err := json.Marshal(req)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:]), nil
}

func (c *Cache) path(key string) string {
	return filepath.Join(c.dir, key+".json")
}

// Get returns the cached value for key, or nil, false.
func (c *Cache) Get(key string) (json.RawMessage, bool) {
	b, err := os.ReadFile(c.path(key))
	if err != nil {
		return nil, false
	}
	return json.RawMessage(b), true
}

// Put stores value under key.
func (c *Cache) Put(key string, value json.RawMessage) error {
	tmp := c.path(key) + ".tmp"
	if err := os.WriteFile(tmp, value, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, c.path(key))
}
