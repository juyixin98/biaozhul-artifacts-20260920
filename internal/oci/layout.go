package oci

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// Edge kinds recorded in the dependency graph.
const (
	EdgeIndex    = "index"    // index -> nested index
	EdgeManifest = "manifest" // index -> manifest
	EdgeConfig   = "config"   // manifest -> config
	EdgeLayer    = "layer"    // manifest -> layer
)

// Node is one verified document/blob in the dependency graph.
type Node struct {
	Digest    string
	MediaType string
	Size      int64
	Raw       []byte // raw JSON for index/manifest/config documents; nil for layers
}

// Edge is a parent->child dependency, with platform info on manifest edges.
type Edge struct {
	Parent   string
	Child    string
	Kind     string
	Position int
	Platform *Platform
}

// Graph is the fully verified dependency graph of one imported index.
type Graph struct {
	Root  string // digest of index.json
	Nodes map[string]*Node
	Edges []Edge
}

// Adjacency returns the parent->children map used for cycle detection.
func (g *Graph) Adjacency() map[string][]string {
	adj := map[string][]string{}
	for _, e := range g.Edges {
		adj[e.Parent] = append(adj[e.Parent], e.Child)
	}
	return adj
}

// Chain returns the ordered dependency chain root -> selected manifest ->
// config -> layers, as (digest, mediaType, kind, size) tuples.
func (g *Graph) Chain(manifestDigest string) ([]Edge, error) {
	var chain []Edge
	chain = append(chain, Edge{Parent: "", Child: g.Root, Kind: "root"})
	foundManifest := false
	var cfgDigest string
	for _, e := range g.Edges {
		if e.Parent == g.Root && e.Child == manifestDigest && e.Kind == EdgeManifest {
			foundManifest = true
		}
	}
	if !foundManifest {
		return nil, fmt.Errorf("manifest %s is not a child of root %s", manifestDigest, g.Root)
	}
	chain = append(chain, Edge{Parent: g.Root, Child: manifestDigest, Kind: EdgeManifest})
	for _, e := range g.Edges {
		if e.Parent == manifestDigest && e.Kind == EdgeConfig {
			cfgDigest = e.Child
			chain = append(chain, e)
		}
	}
	if cfgDigest == "" {
		return nil, fmt.Errorf("manifest %s has no config edge", manifestDigest)
	}
	for _, e := range g.Edges {
		if e.Parent == manifestDigest && e.Kind == EdgeLayer {
			chain = append(chain, e)
		}
	}
	return chain, nil
}

// ImportLayout reads an OCI image layout directory (index.json + blobs/),
// verifies the digest and size of the index, every manifest, every config
// and every layer, checks that each config's platform matches its manifest
// descriptor, and returns the verified dependency graph. Any mismatch fails
// the whole import with a descriptive error; nothing is half-verified.
func ImportLayout(dir string) (*Graph, error) {
	indexPath := filepath.Join(dir, "index.json")
	raw, err := os.ReadFile(indexPath)
	if err != nil {
		return nil, fmt.Errorf("read index.json: %w", err)
	}
	g := &Graph{Root: DigestBytes(raw), Nodes: map[string]*Node{}}

	visiting := map[string]bool{}
	if err := importIndex(dir, g, g.Root, raw, "", visiting); err != nil {
		return nil, err
	}
	if err := DetectCycle(g.Adjacency()); err != nil {
		return nil, err
	}
	return g, nil
}

func blobPath(dir, digest string) (string, error) {
	_, hexPart, err := ParseDigest(digest)
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "blobs", "sha256", hexPart), nil
}

// verifyAndRead verifies a blob's digest+size, then returns its content.
func verifyAndRead(dir string, d Descriptor) ([]byte, error) {
	p, err := blobPath(dir, d.Digest)
	if err != nil {
		return nil, err
	}
	if _, err := VerifyBlob(p, d.Digest, d.Size); err != nil {
		return nil, err
	}
	raw, err := os.ReadFile(p)
	if err != nil {
		return nil, fmt.Errorf("read verified blob %s: %w", d.Digest, err)
	}
	return raw, nil
}

func importIndex(dir string, g *Graph, digest string, raw []byte, parent string, visiting map[string]bool) error {
	if visiting[digest] {
		return fmt.Errorf("circular reference detected at index %s", digest)
	}
	_, done := g.Nodes[digest]
	if !done {
		var idx Index
		if err := json.Unmarshal(raw, &idx); err != nil {
			return fmt.Errorf("parse index %s: %w", digest, err)
		}
		mt := idx.MediaType
		if mt == "" {
			mt = MediaTypeIndexOCI
		}
		if !IsIndexMediaType(mt) {
			return fmt.Errorf("index %s: unexpected mediaType %q", digest, mt)
		}
		g.Nodes[digest] = &Node{Digest: digest, MediaType: mt, Size: int64(len(raw)), Raw: raw}
		if len(idx.Manifests) == 0 {
			return fmt.Errorf("index %s: no manifests", digest)
		}
		visiting[digest] = true
		defer delete(visiting, digest)

		for i, desc := range idx.Manifests {
			if desc.Platform == nil && IsManifestMediaType(desc.MediaType) {
				return fmt.Errorf("index %s: manifest entry %d (%s) has no platform", digest, i, desc.Digest)
			}
			raw, err := verifyAndRead(dir, desc)
			if err != nil {
				return fmt.Errorf("index %s entry %d: %w", digest, i, err)
			}
			switch {
			case IsIndexMediaType(desc.MediaType):
				if err := importIndex(dir, g, desc.Digest, raw, digest, visiting); err != nil {
					return err
				}
			case IsManifestMediaType(desc.MediaType):
				if err := importManifest(dir, g, desc, raw, digest, i); err != nil {
					return err
				}
			default:
				return fmt.Errorf("index %s entry %d: unsupported mediaType %q", digest, i, desc.MediaType)
			}
		}
	}
	if parent != "" && !g.hasEdge(parent, digest, EdgeIndex, 0) {
		g.Edges = append(g.Edges, Edge{Parent: parent, Child: digest, Kind: EdgeIndex})
	}
	return nil
}

func (g *Graph) hasEdge(parent, child, kind string, position int) bool {
	for _, e := range g.Edges {
		if e.Parent == parent && e.Child == child && e.Kind == kind && e.Position == position {
			return true
		}
	}
	return false
}

func importManifest(dir string, g *Graph, desc Descriptor, raw []byte, parent string, pos int) error {
	digest := desc.Digest
	manifestEdge := Edge{Parent: parent, Child: digest, Kind: EdgeManifest, Position: pos, Platform: desc.Platform}
	if _, done := g.Nodes[digest]; !done {
		var m Manifest
		if err := json.Unmarshal(raw, &m); err != nil {
			return fmt.Errorf("parse manifest %s: %w", digest, err)
		}
		g.Nodes[digest] = &Node{Digest: digest, MediaType: desc.MediaType, Size: desc.Size, Raw: raw}

		// Config: verify digest+size, parse, and cross-check the platform the
		// config claims against the platform the index descriptor advertises.
		cfgRaw, err := verifyAndRead(dir, m.Config)
		if err != nil {
			return fmt.Errorf("manifest %s config: %w", digest, err)
		}
		var cfg Config
		if err := json.Unmarshal(cfgRaw, &cfg); err != nil {
			return fmt.Errorf("manifest %s: parse config %s: %w", digest, m.Config.Digest, err)
		}
		if desc.Platform != nil {
			if cfg.OS != desc.Platform.OS ||
				cfg.Architecture != desc.Platform.Architecture ||
				cfg.Variant != desc.Platform.Variant {
				return fmt.Errorf("manifest %s: config platform %s/%s/%q does not match descriptor platform %s/%s/%q",
					digest, cfg.OS, cfg.Architecture, cfg.Variant,
					desc.Platform.OS, desc.Platform.Architecture, desc.Platform.Variant)
			}
		}
		g.Nodes[m.Config.Digest] = &Node{Digest: m.Config.Digest, MediaType: m.Config.MediaType, Size: m.Config.Size, Raw: cfgRaw}
		g.Edges = append(g.Edges, Edge{Parent: digest, Child: m.Config.Digest, Kind: EdgeConfig})

		// Layers: every referenced blob must exist and match digest+size.
		for i, layer := range m.Layers {
			p, err := blobPath(dir, layer.Digest)
			if err != nil {
				return fmt.Errorf("manifest %s layer %d: %w", digest, i, err)
			}
			size, err := VerifyBlob(p, layer.Digest, layer.Size)
			if err != nil {
				return fmt.Errorf("manifest %s layer %d: %w", digest, i, err)
			}
			if _, done := g.Nodes[layer.Digest]; !done {
				g.Nodes[layer.Digest] = &Node{Digest: layer.Digest, MediaType: layer.MediaType, Size: size}
			}
			g.Edges = append(g.Edges, Edge{Parent: digest, Child: layer.Digest, Kind: EdgeLayer, Position: i})
		}
	}
	if !g.hasEdge(parent, digest, EdgeManifest, pos) {
		g.Edges = append(g.Edges, manifestEdge)
	}
	return nil
}
