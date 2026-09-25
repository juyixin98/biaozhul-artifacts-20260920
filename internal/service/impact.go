package service

import (
	"sort"

	"buildprovenance/internal/provenance"
)

// ImpactReport answers: if sourcePath changes, which recorded outputs are
// built on it (transitively)? Downstream digests are derived from their
// inputs, so all such outputs are stale once the source content changes.
type ImpactReport struct {
	SourcePath     string   `json:"sourcePath"`
	CurrentDigest  string   `json:"currentDigest,omitempty"` // digest currently registered at this logical path
	QueriedDigest  string   `json:"queriedDigest,omitempty"` // digest the query was made against, if given
	Affected       []string `json:"affected"`                // artifactIDs whose recorded chain contains this source (exact digest)
	AllDescendants []string `json:"allDescendants"`          // ...and those built from the logical path at any digest
	DirectOnly     []string `json:"directOnly,omitempty"`    // artifacts consuming the source directly
}

// Impact computes the reverse provenance closure for a source. When digest is
// empty (""), Affected lists artifacts attached to whatever digest is
// currently registered; otherwise the exact recorded digest is matched.
func (s *Service) Impact(sourcePath, digest string) (*ImpactReport, error) {
	rep := &ImpactReport{SourcePath: sourcePath, QueriedDigest: digest}
	if src, err := s.sources.Get(sourcePath); err == nil {
		rep.CurrentDigest = string(src.Digest)
		if digest == "" {
			digest = string(src.Digest)
		}
	}

	// For each artifact record, walk upstream collecting the set of source
	// (path,digest) pairs it transitively depends on.
	all := s.log.All()
	records := map[string]provenance.Record{}
	ids := make([]string, 0, len(all))
	for _, r := range all {
		records[r.ArtifactID] = r
		ids = append(ids, r.ArtifactID)
	}
	sort.Strings(ids)

	// memo: artifactID -> sources map "path@digest" ; -1 style cycle guard
	type cacheEntry struct {
		sources map[string]struct{}
		done    bool
	}
	cache := map[string]*cacheEntry{}
	visiting := map[string]bool{}

	var sourcesOf func(id string) map[string]struct{}
	sourcesOf = func(id string) map[string]struct{} {
		if e, ok := cache[id]; ok && e.done {
			return e.sources
		}
		if visiting[id] {
			return map[string]struct{}{} // forged cycle; verify reports it, impact stays finite
		}
		visiting[id] = true
		r, ok := records[id]
		out := map[string]struct{}{}
		if ok {
			for _, in := range r.Inputs {
				if in.Kind == "source" {
					out[string(in.Path)+"@"+string(in.Digest)] = struct{}{}
				}
			}
			for up := range r.Upstreams {
				for k := range sourcesOf(up) {
					out[k] = struct{}{}
				}
			}
		}
		visiting[id] = false
		cache[id] = &cacheEntry{sources: out, done: true}
		return out
	}

	exactKey := sourcePath + "@" + digest
	pathPrefix := sourcePath + "@"
	for _, id := range ids {
		r := records[id]
		set := sourcesOf(id)
		if _, ok := set[exactKey]; ok && digest != "" {
			rep.Affected = append(rep.Affected, id)
		}
		for k := range set {
			if len(k) >= len(pathPrefix) && k[:len(pathPrefix)] == pathPrefix {
				rep.AllDescendants = append(rep.AllDescendants, id)
				break
			}
		}
		for _, in := range r.Inputs {
			if in.Kind == "source" && in.Path == sourcePath && (digest == "" || string(in.Digest) == digest) {
				rep.DirectOnly = append(rep.DirectOnly, id)
			}
		}
	}
	sort.Strings(rep.Affected)
	sort.Strings(rep.AllDescendants)
	sort.Strings(rep.DirectOnly)
	return rep, nil
}

// ProvenanceNode is one node in a returned provenance tree.
type ProvenanceNode struct {
	ArtifactID string                `json:"artifactId"`
	Tool       string                `json:"tool"`
	ToolDigest provenance.Digest     `json:"toolDigest"`
	Digest     provenance.Digest     `json:"digest"`
	Sources    []provenance.InputRef `json:"sourceInputs,omitempty"`
	Upstreams  []ProvenanceNode      `json:"upstreams,omitempty"`
}

// ProvenanceTree returns the transitive provenance of an artifact as a tree
// (cycles broken and annotated). Useful alongside verify/impact.
func (s *Service) ProvenanceTree(artifactID string) (*ProvenanceNode, error) {
	visiting := map[string]bool{}
	var build func(id string) *ProvenanceNode
	build = func(id string) *ProvenanceNode {
		art, err := s.artifacts.Get(id)
		n := &ProvenanceNode{ArtifactID: id, Digest: art.Digest}
		if err != nil {
			return n
		}
		rec, err := s.log.Get(id)
		if err != nil {
			return n
		}
		n.Tool = rec.ToolName
		n.ToolDigest = rec.ToolDigest
		up := make([]string, 0, len(rec.Upstreams))
		for _, in := range rec.Inputs {
			if in.Kind == "source" {
				n.Sources = append(n.Sources, in)
			}
		}
		sort.Slice(n.Sources, func(i, j int) bool { return n.Sources[i].Slot < n.Sources[j].Slot })
		for u := range rec.Upstreams {
			up = append(up, u)
		}
		sort.Strings(up)
		if visiting[id] {
			n.Upstreams = append(n.Upstreams, ProvenanceNode{ArtifactID: "[cycle] " + id})
			return n
		}
		visiting[id] = true
		for _, u := range up {
			child := build(u)
			n.Upstreams = append(n.Upstreams, *child)
		}
		visiting[id] = false
		return n
	}
	root := build(artifactID)
	if root == nil {
		return nil, provenance.ErrNotFound
	}
	return root, nil
}
