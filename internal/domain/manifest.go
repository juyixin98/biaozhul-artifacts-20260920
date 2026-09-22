// Package domain defines the composition manifest and its validation rules.
package domain

import (
	"fmt"
	"sort"
	"strings"
)

// Manifest is the JSON description of a composition. Layers are alpha
// composited in explicit Order (independent of map iteration order); each
// layer may depend on other layers only to express a DAG, while Order is the
// authoritative paint order. A layer references an uploaded asset by its
// project-relative path; at version-freeze time the path is resolved to an
// asset id + sha256 digest.
type Manifest struct {
	Width  int            `json:"width"`
	Height int            `json:"height"`
	Layers []Layer        `json:"layers"`
	Frames FrameRange     `json:"frames"`
	Meta   map[string]any `json:"meta,omitempty"`
}

// FrameRange is advisory metadata in the manifest; jobs carry the concrete
// frame interval they render.
type FrameRange struct {
	Start int `json:"start"`
	End   int `json:"end"`
}

// Layer is one PNG layer. X/Y place the layer's top-left corner on the
// canvas. Per-frame x/y overrides may be given for animation.
type Layer struct {
	ID           string      `json:"id"`
	AssetPath    string      `json:"asset"`
	X            int         `json:"x"`
	Y            int         `json:"y"`
	Opacity      float64     `json:"opacity,omitempty"` // 0..1, default 1
	Deps         []string    `json:"deps,omitempty"`
	FrameOffsetX map[int]int `json:"frameOffsetX,omitempty"`
	FrameOffsetY map[int]int `json:"frameOffsetY,omitempty"`
}

// ResolvedAsset is one layer's binding at freeze time.
type ResolvedAsset struct {
	LayerID string
	AssetID string
	SHA256  string
	Width   int
	Height  int
}

// AssetFetcher resolves a project-relative asset path to its stored digest.
// It is implemented by the API layer against the database.
type AssetFetcher func(relPath string) (assetID, sha256 string, width, height int, err error)

// Validate checks the manifest without touching the database: structure,
// bounds and the dependency DAG. Resource existence is resolved separately
// via Resolve so callers get distinct errors.
func (m *Manifest) Validate() error {
	if m.Width <= 0 || m.Height <= 0 {
		return fmt.Errorf("canvas size must be positive, got %dx%d", m.Width, m.Height)
	}
	if len(m.Layers) == 0 {
		return fmt.Errorf("composition has no layers")
	}
	seen := make(map[string]bool, len(m.Layers))
	for i, l := range m.Layers {
		if l.ID == "" {
			return fmt.Errorf("layer at index %d is missing an id", i)
		}
		if seen[l.ID] {
			return fmt.Errorf("duplicate layer id %q", l.ID)
		}
		if l.AssetPath == "" {
			return fmt.Errorf("layer %q is missing an asset path", l.ID)
		}
		if err := ValidateRelPath(l.AssetPath); err != nil {
			return fmt.Errorf("layer %q: %w", l.ID, err)
		}
		if l.Opacity < 0 || l.Opacity > 1 {
			return fmt.Errorf("layer %q: opacity must be within [0,1]", l.ID)
		}
		seen[l.ID] = true
	}

	deps := make(map[string][]string, len(m.Layers))
	for _, l := range m.Layers {
		for _, d := range l.Deps {
			if d == l.ID {
				return fmt.Errorf("layer %q depends on itself", l.ID)
			}
			if !seen[d] {
				return fmt.Errorf("layer %q depends on missing layer %q", l.ID, d)
			}
		}
		deps[l.ID] = l.Deps
	}
	if cycle := findCycle(deps); cycle != nil {
		return fmt.Errorf("dependency cycle: %s", strings.Join(cycle, " -> "))
	}
	return nil
}

// Resolve validates the manifest and binds every layer to a concrete asset
// digest. Missing resources fail here. The returned slice is sorted by layer
// id for deterministic storage.
func (m *Manifest) Resolve(fetch AssetFetcher) ([]ResolvedAsset, error) {
	if err := m.Validate(); err != nil {
		return nil, err
	}
	out := make([]ResolvedAsset, 0, len(m.Layers))
	for _, l := range m.Layers {
		id, digest, w, h, err := fetch(l.AssetPath)
		if err != nil {
			return nil, fmt.Errorf("layer %q: %w", l.ID, err)
		}
		// Layer pixels must not escape the canvas.
		if l.X < 0 || l.Y < 0 || l.X+w > m.Width || l.Y+h > m.Height {
			return nil, fmt.Errorf("layer %q out of canvas bounds: asset %dx%d at (%d,%d) on canvas %dx%d",
				l.ID, w, h, l.X, l.Y, m.Width, m.Height)
		}
		for fx, dx := range l.FrameOffsetX {
			if l.X+dx < 0 || l.X+dx+w > m.Width {
				return nil, fmt.Errorf("layer %q out of canvas bounds on frame %d (x offset %d)", l.ID, fx, dx)
			}
		}
		for fy, dy := range l.FrameOffsetY {
			if l.Y+dy < 0 || l.Y+dy+h > m.Height {
				return nil, fmt.Errorf("layer %q out of canvas bounds on frame %d (y offset %d)", l.ID, fy, dy)
			}
		}
		out = append(out, ResolvedAsset{
			LayerID: l.ID, AssetID: id, SHA256: digest, Width: w, Height: h,
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].LayerID < out[j].LayerID })
	return out, nil
}

// LayerByID returns the layer with the given id.
func (m *Manifest) LayerByID(id string) (Layer, bool) {
	for _, l := range m.Layers {
		if l.ID == id {
			return l, true
		}
	}
	return Layer{}, false
}

// findCycle runs Kahn's algorithm; if vertices remain after peeling, it
// reports a representative cycle path.
func findCycle(deps map[string][]string) []string {
	indeg := make(map[string]int, len(deps))
	for n := range deps {
		indeg[n] = 0
	}
	for _, ds := range deps {
		for _, d := range ds {
			indeg[d]++
		}
	}
	var queue []string
	for n, d := range indeg {
		if d == 0 {
			queue = append(queue, n)
		}
	}
	visited := 0
	for len(queue) > 0 {
		n := queue[0]
		queue = queue[1:]
		visited++
		for _, d := range deps[n] {
			indeg[d]--
			if indeg[d] == 0 {
				queue = append(queue, d)
			}
		}
	}
	if visited == len(deps) {
		return nil
	}
	// Something is left in the graph: walk it to produce a readable cycle.
	var onCycle string
	for n, d := range indeg {
		if d > 0 {
			onCycle = n
			break
		}
	}
	seen := map[string]bool{}
	path := []string{}
	cur := onCycle
	for !seen[cur] {
		seen[cur] = true
		path = append(path, cur)
		cur = deps[cur][0]
	}
	path = append(path, cur)
	return path
}
