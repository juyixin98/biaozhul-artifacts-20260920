// Package impact answers "which outputs are affected if this source changes?".
// It walks the recorded provenance graph in reverse: from the actions that
// directly read the source, through every transitive consumer, collecting
// the produced outputs and example dependency chains.
package impact

import (
	"sort"

	"bis/internal/digest"
	"bis/internal/provenance"
	"bis/internal/spec"
	"bis/internal/store"
)

// Match identifies how a source was matched (by path and/or digest).
type Match struct {
	Path   string        `json:"path,omitempty"`
	Digest digest.Digest `json:"digest,omitempty"`
}

// AffectedOutput is one artifact that depends (transitively) on the source.
type AffectedOutput struct {
	ActionID string        `json:"action_id"`
	Output   string        `json:"output"`
	Digest   digest.Digest `json:"digest"`
	Distance int           `json:"distance"` // 1 = directly reads the source
}

// Chain is an example source -> output dependency path.
type Chain struct {
	SourcePath string   `json:"source_path"`
	Path       []string `json:"path"` // action ids from reader to producer
	Output     string   `json:"output"`
}

// Report is the impact analysis result.
type Report struct {
	Project         string           `json:"project"`
	Query           Match            `json:"query"`
	MatchedSources  []string         `json:"matched_sources"`
	AffectedActions []string         `json:"affected_actions"`
	AffectedOutputs []AffectedOutput `json:"affected_outputs"`
	Chains          []Chain          `json:"chains"`
}

// Analyze computes the blast radius of a source identified by path and/or
// digest against the current provenance records.
func Analyze(st *store.Store, p *spec.Project, path string, wantDigest *digest.Digest) *Report {
	rep := &Report{Project: p.Name, Query: Match{Path: path}}
	if wantDigest != nil {
		rep.Query.Digest = *wantDigest
	}

	idx := st.LoadIndex(p.Name)

	// Load the record of every action with an indexed record.
	recs := map[string]*provenance.Record{}
	for _, a := range p.Actions {
		ent, ok := idx.Records[a.ID]
		if !ok {
			continue
		}
		if r, err := st.GetRecord(ent.RecordID); err == nil {
			recs[a.ID] = r
		}
	}

	// Direct readers + the concrete matched source paths.
	direct := map[string]bool{}
	matched := map[string]bool{}
	for _, a := range p.Actions {
		r, ok := recs[a.ID]
		if !ok {
			continue
		}
		for _, s := range r.Sources {
			if (path == "" || s.Path == path) && (wantDigest == nil || s.Digest.Equal(*wantDigest)) {
				direct[a.ID] = true
				matched[s.Path] = true
			}
		}
	}

	// Reverse edges from the recorded graph (action -> consumers).
	consumers := map[string]map[string]bool{}
	for id, r := range recs {
		seen := map[string]bool{}
		for _, u := range r.Upstreams {
			if seen[u.ActionID] {
				continue
			}
			seen[u.ActionID] = true
			if consumers[u.ActionID] == nil {
				consumers[u.ActionID] = map[string]bool{}
			}

			consumers[u.ActionID][id] = true
		}
	}

	// BFS: distance and one predecessor path per affected action.
	dist := map[string]int{}
	parent := map[string]string{}
	var queue []string
	for id := range direct {
		dist[id] = 1
		queue = append(queue, id)
	}
	sort.Strings(queue) // deterministic processing
	for len(queue) > 0 {
		id := queue[0]
		queue = queue[1:]
		cs := mapKeys(consumers[id])
		sort.Strings(cs)
		for _, c := range cs {
			if _, seen := dist[c]; !seen {
				dist[c] = dist[id] + 1
				parent[c] = id
				queue = append(queue, c)
			}
		}
	}

	actions := keysInt(dist)
	sort.Strings(actions)
	rep.AffectedActions = actions
	rep.MatchedSources = mapKeys(matched)
	sort.Strings(rep.MatchedSources)

	for _, id := range actions {
		r := recs[id]
		outs := append([]provenance.OutputRef(nil), r.Outputs...)
		sort.Slice(outs, func(i, j int) bool { return outs[i].Path < outs[j].Path })
		for _, o := range outs {
			rep.AffectedOutputs = append(rep.AffectedOutputs, AffectedOutput{
				ActionID: id, Output: o.Path, Digest: o.Digest, Distance: dist[id],
			})
		}
	}

	// One example chain per (direct reader, final output).
	sources := append([]string(nil), rep.MatchedSources...)
	for _, reader := range actions {
		if !direct[reader] {
			continue
		}
		// reachable outputs rooted at this reader
		r := recs[reader]
		for _, o := range r.Outputs {
			rep.Chains = append(rep.Chains, buildChain(sources[0], reader, reader, o.Path, parent))
		}
		// extend through consumers for one chain to each leaf output
		var stack []string
		stack = append(stack, reader)
		for len(stack) > 0 {
			cur := stack[0]
			stack = stack[1:]
			cs := mapKeys(consumers[cur])
			if len(cs) == 0 {
				continue
			}
			for _, c := range cs {
				stack = append(stack, c)
				if !direct[c] {
					for _, o := range recs[c].Outputs {
						rep.Chains = append(rep.Chains, buildChain(sources[0], reader, c, o.Path, parent))
					}
				}
			}
		}
	}

	// Deduplicate chains, cap noise by sorting.
	rep.Chains = dedupChains(rep.Chains)
	return rep
}

func buildChain(sourcePath, root, end, output string, parent map[string]string) Chain {
	var rev []string
	cur := end
	for cur != root {
		rev = append(rev, cur)
		p, ok := parent[cur]
		if !ok {
			break
		}
		cur = p
	}
	rev = append(rev, root)
	path := make([]string, 0, len(rev))
	for i := len(rev) - 1; i >= 0; i-- {
		path = append(path, rev[i])
	}
	return Chain{SourcePath: sourcePath, Path: path, Output: output}
}

func mapKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func keysInt(m map[string]int) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func dedupChains(in []Chain) []Chain {
	type key struct {
		s, o string
		p    string
	}
	seen := map[key]bool{}
	out := in[:0:0]
	for _, c := range in {
		k := key{s: c.SourcePath, o: c.Output, p: joinPath(c.Path)}
		if seen[k] {
			continue
		}
		seen[k] = true
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].SourcePath != out[j].SourcePath {
			return out[i].SourcePath < out[j].SourcePath
		}
		if out[i].Output != out[j].Output {
			return out[i].Output < out[j].Output
		}
		return joinPath(out[i].Path) < joinPath(out[j].Path)
	})
	return out
}

func joinPath(p []string) string {
	out := ""
	for _, x := range p {
		out += x + ">"
	}
	return out
}
