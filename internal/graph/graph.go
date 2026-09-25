// Package graph holds a file dependency graph with deterministic output.
package graph

import "sort"

// Graph maps each file to its sorted direct dependencies.
// Keys and values are root-relative slash paths.
type Graph struct {
	Deps map[string][]string `json:"deps"`
}

// New returns an empty graph.
func New() *Graph {
	return &Graph{Deps: map[string][]string{}}
}

// Set replaces the outgoing edges of file with deps (deduped, sorted).
func (g *Graph) Set(file string, deps []string) {
	seen := map[string]bool{}
	out := make([]string, 0, len(deps))
	for _, d := range deps {
		// Self-edges are kept: a self-include with include guards is legal
		// C and is reported as a one-node cycle by Cycles.
		if seen[d] {
			continue
		}
		seen[d] = true
		out = append(out, d)
	}
	sort.Strings(out)
	g.Deps[file] = out
}

// Remove deletes the given nodes and all inbound edges to them.
func (g *Graph) Remove(files ...string) {
	gone := map[string]bool{}
	for _, f := range files {
		gone[f] = true
		delete(g.Deps, f)
	}
	for k, deps := range g.Deps {
		kept := deps[:0]
		for _, d := range deps {
			if !gone[d] {
				kept = append(kept, d)
			}
		}
		g.Deps[k] = kept
	}
}

// Files returns all node keys, sorted.
func (g *Graph) Files() []string {
	out := make([]string, 0, len(g.Deps))
	for f := range g.Deps {
		out = append(out, f)
	}
	sort.Strings(out)
	return out
}

// Affected returns the transitive reverse closure of changed: every file
// that directly or transitively includes any of them. The changed files
// themselves are included when present in the graph. Output is sorted.
func (g *Graph) Affected(changed []string) []string {
	rev := map[string][]string{}
	for from, deps := range g.Deps {
		for _, to := range deps {
			rev[to] = append(rev[to], from)
		}
	}
	seen := map[string]bool{}
	stack := append([]string(nil), changed...)
	for len(stack) > 0 {
		f := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if seen[f] {
			continue
		}
		seen[f] = true
		stack = append(stack, rev[f]...)
	}
	out := make([]string, 0, len(seen))
	for f := range seen {
		out = append(out, f)
	}
	sort.Strings(out)
	return out
}

// Cycles returns the dependency cycles in the graph, each reported once,
// normalized to start at its lexicographically smallest node, with the
// list itself sorted. Deterministic for identical input.
func (g *Graph) Cycles() [][]string {
	const (
		white = 0 // unvisited
		gray  = 1 // on the current DFS stack
		black = 2 // done
	)
	state := map[string]int{}
	var stack []string
	found := map[string][]string{} // key: normalized cycle joined by \x00

	var visit func(f string)
	visit = func(f string) {
		state[f] = gray
		stack = append(stack, f)
		for _, dep := range g.Deps[f] {
			switch state[dep] {
			case white:
				visit(dep)
			case gray:
				// Back edge: extract the cycle from the stack.
				start := 0
				for i, s := range stack {
					if s == dep {
						start = i
						break
					}
				}
				cycle := normalize(append([]string(nil), stack[start:]...))
				key := ""
				for _, n := range cycle {
					key += n + "\x00"
				}
				found[key] = cycle
			}
		}
		stack = stack[:len(stack)-1]
		state[f] = black
	}

	for _, f := range g.Files() {
		if state[f] == white {
			visit(f)
		}
	}
	out := make([][]string, 0, len(found))
	for _, c := range found {
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool {
		for k := 0; k < len(out[i]) && k < len(out[j]); k++ {
			if out[i][k] != out[j][k] {
				return out[i][k] < out[j][k]
			}
		}
		return len(out[i]) < len(out[j])
	})
	return out
}

// normalize rotates a cycle so its smallest element comes first.
func normalize(cycle []string) []string {
	min := 0
	for i, n := range cycle {
		if n < cycle[min] {
			min = i
		}
	}
	return append(append([]string(nil), cycle[min:]...), cycle[:min]...)
}
