// Package compositor validates composition specs and performs real local PNG
// layer compositing: per-frame alpha (source-over) blending in layer order.
package compositor

import (
	"bytes"
	"encoding/json"
	"fmt"
)

// Spec is the immutable description of a composition version.
type Spec struct {
	CanvasWidth  int     `json:"canvas_width"`
	CanvasHeight int     `json:"canvas_height"`
	FrameCount   int     `json:"frame_count"`
	Layers       []Layer `json:"layers"`
}

// Layer describes one PNG layer. Deps lists layer ids that must be drawn
// before this one; the actual draw order is a topological ordering in which
// each layer appears after all of its dependencies.
type Layer struct {
	ID      string `json:"id"`
	AssetID string `json:"asset_id"` // resolved to a digest when a version is frozen
	X       int    `json:"x"`
	Y       int    `json:"y"`
	// FrameStart/FrameEnd limit the frames on which the layer is visible
	// (inclusive). Zero values default to the full range.
	FrameStart int      `json:"frame_start,omitempty"`
	FrameEnd   int      `json:"frame_end,omitempty"`
	Deps       []string `json:"deps,omitempty"`
}

const (
	maxCanvasSize = 16384
	maxFrameCount = 100000
	maxLayers     = 256
	maxLayerIDLen = 120
)

// Validate checks structural constraints only. assetExists reports whether an
// asset id belongs to the owning project; rendering-time digest checks happen
// at work time.
func (s *Spec) Validate(assetExists func(assetID string) bool) error {
	if s.CanvasWidth <= 0 || s.CanvasHeight <= 0 ||
		s.CanvasWidth > maxCanvasSize || s.CanvasHeight > maxCanvasSize {
		return fmt.Errorf("canvas dimensions must be within 1..%d", maxCanvasSize)
	}
	if s.FrameCount <= 0 || s.FrameCount > maxFrameCount {
		return fmt.Errorf("frame_count must be within 1..%d", maxFrameCount)
	}
	if len(s.Layers) == 0 {
		return fmt.Errorf("composition must contain at least one layer")
	}
	if len(s.Layers) > maxLayers {
		return fmt.Errorf("too many layers (%d > %d)", len(s.Layers), maxLayers)
	}
	seen := make(map[string]int, len(s.Layers))
	for i, l := range s.Layers {
		if l.ID == "" || len(l.ID) > maxLayerIDLen {
			return fmt.Errorf("layer %d: id required (max %d chars)", i, maxLayerIDLen)
		}
		if prev, dup := seen[l.ID]; dup {
			return fmt.Errorf("duplicate layer id %q (layers %d and %d)", l.ID, prev, i)
		}
		seen[l.ID] = i
		if l.AssetID == "" {
			return fmt.Errorf("layer %q: asset_id required", l.ID)
		}
		if !assetExists(l.AssetID) {
			return fmt.Errorf("layer %q: asset %s not found in project (missing resource)", l.ID, l.AssetID)
		}
		if l.X < 0 || l.Y < 0 || l.X > s.CanvasWidth || l.Y > s.CanvasHeight {
			return fmt.Errorf("layer %q: origin (%d,%d) outside canvas %dx%d",
				l.ID, l.X, l.Y, s.CanvasWidth, s.CanvasHeight)
		}
		// Default frame window, then validate.
		fs, fe := l.FrameStart, l.FrameEnd
		if fs == 0 && fe == 0 {
			fe = s.FrameCount - 1
		}
		if fs < 0 || fe < fs || fe >= s.FrameCount {
			return fmt.Errorf("layer %q: frame window [%d,%d] invalid for frame_count %d",
				l.ID, fs, fe, s.FrameCount)
		}
	}
	// Dependency edges must reference known layers and be acyclic.
	for _, l := range s.Layers {
		for _, d := range l.Deps {
			if _, ok := seen[d]; !ok {
				return fmt.Errorf("layer %q depends on unknown layer %q", l.ID, d)
			}
			if d == l.ID {
				return fmt.Errorf("layer %q depends on itself", l.ID)
			}
		}
	}
	if _, err := s.DrawOrder(); err != nil {
		return err
	}
	return nil
}

// DrawOrder returns layer indices in dependency order (Kahn's algorithm).
// It rejects cycles. Stable: when ready-set has several options, the lowest
// original index is chosen so two renders of the same version draw identically.
func (s *Spec) DrawOrder() ([]int, error) {
	n := len(s.Layers)
	idx := make(map[string]int, n)
	for i, l := range s.Layers {
		idx[l.ID] = i
	}
	indeg := make([]int, n)
	adj := make([][]int, n)
	for i, l := range s.Layers {
		for _, d := range l.Deps {
			j := idx[d]
			adj[j] = append(adj[j], i)
			indeg[i]++
		}
	}
	// Kahn with a min-index "queue" for stability.
	ready := make([]int, 0, n)
	for i := 0; i < n; i++ {
		if indeg[i] == 0 {
			ready = append(ready, i)
		}
	}
	order := make([]int, 0, n)
	for len(ready) > 0 {
		// pick smallest
		pick := 0
		for k := 1; k < len(ready); k++ {
			if ready[k] < ready[pick] {
				pick = k
			}
		}
		v := ready[pick]
		ready = append(ready[:pick], ready[pick+1:]...)
		order = append(order, v)
		for _, w := range adj[v] {
			indeg[w]--
			if indeg[w] == 0 {
				ready = append(ready, w)
			}
		}
	}
	if len(order) != n {
		return nil, fmt.Errorf("dependency cycle detected among layers")
	}
	return order, nil
}

// LayerVisible reports whether the layer is present on frame f.
func (l Layer) Visible(f int) bool {
	fs, fe := l.FrameStart, l.FrameEnd
	if fs == 0 && fe == 0 {
		return true
	}
	return f >= fs && f <= fe
}

// CanonicalJSON round-trips the spec through encoding/json to produce stable
// bytes for hashing (keys sorted by encoding/json for maps; our struct field
// order is fixed).
func CanonicalJSON(s *Spec) ([]byte, error) {
	b, err := json.Marshal(s)
	if err != nil {
		return nil, err
	}
	// Normalize whitespace/roundtrip.
	var v any
	if err := json.Unmarshal(b, &v); err != nil {
		return nil, err
	}
	var out bytes.Buffer
	enc := json.NewEncoder(&out)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimSpace(out.Bytes()), nil
}
