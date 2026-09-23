// Package registry holds the set of named conversion mappings and the
// atomically-swapped active one, enabling runtime version hot-switching.
package registry

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"sync"
	"sync/atomic"

	"github.com/p079/telegw/internal/convert"
)

// Registry is safe for concurrent use.
type Registry struct {
	active   atomic.Value // convert.Mapping
	mu       sync.RWMutex
	loaded   map[string]convert.Mapping
	filePath string
}

// Load reads mappings from a JSON file ({"mappings": [...]}) and
// activates initialVersion (or the first entry if empty).
func Load(filePath, initialVersion string) (*Registry, error) {
	r := &Registry{filePath: filePath, loaded: map[string]convert.Mapping{}}
	if err := r.reloadFromFile(); err != nil {
		return nil, err
	}
	if initialVersion == "" {
		for v := range r.loaded {
			initialVersion = v
			break
		}
	}
	if err := r.Activate(initialVersion); err != nil {
		return nil, err
	}
	return r, nil
}

// NewInMemory builds a registry from explicit mappings (used in tests).
func NewInMemory(initial string, mappings ...convert.Mapping) (*Registry, error) {
	r := &Registry{loaded: map[string]convert.Mapping{}}
	for _, m := range mappings {
		if err := m.Validate(); err != nil {
			return nil, err
		}
		r.loaded[m.Version] = m
	}
	if err := r.Activate(initial); err != nil {
		return nil, err
	}
	return r, nil
}

func (r *Registry) reloadFromFile() error {
	raw, err := os.ReadFile(r.filePath)
	if err != nil {
		return fmt.Errorf("read mappings: %w", err)
	}
	var doc struct {
		Mappings []convert.Mapping `json:"mappings"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return fmt.Errorf("parse mappings: %w", err)
	}
	if len(doc.Mappings) == 0 {
		return fmt.Errorf("no mappings in %s", r.filePath)
	}
	fresh := map[string]convert.Mapping{}
	for _, m := range doc.Mappings {
		if err := m.Validate(); err != nil {
			return fmt.Errorf("mapping %q: %w", m.Version, err)
		}
		fresh[m.Version] = m
	}
	r.mu.Lock()
	r.loaded = fresh
	r.mu.Unlock()
	return nil
}

// Active returns the currently active mapping.
func (r *Registry) Active() convert.Mapping {
	return r.active.Load().(convert.Mapping)
}

// Activate hot-swaps the active mapping. An empty version reloads the
// backing file (if any) and keeps the current version when possible.
func (r *Registry) Activate(version string) error {
	if version == "" && r.filePath != "" {
		cur := ""
		if a := r.active.Load(); a != nil {
			cur = a.(convert.Mapping).Version
		}
		if err := r.reloadFromFile(); err != nil {
			return err
		}
		version = cur
	}
	r.mu.RLock()
	m, ok := r.loaded[version]
	r.mu.RUnlock()
	if !ok {
		return fmt.Errorf("mapping version %q not loaded (available: %v)", version, r.Versions())
	}
	r.active.Store(m)
	return nil
}

// Versions lists loaded mapping versions, sorted.
func (r *Registry) Versions() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]string, 0, len(r.loaded))
	for v := range r.loaded {
		out = append(out, v)
	}
	sort.Strings(out)
	return out
}
