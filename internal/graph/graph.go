// Package graph defines the build-graph specification: nodes, edges,
// validation and topological analysis. A graph is a directed acyclic graph
// (DAG) of nodes where an edge A -> B means "A depends on B".
package graph

import (
	"fmt"
	"path"
	"sort"
	"strings"
)

// Tool describes an external command line tool a node relies on.
type Tool struct {
	// Name is the human-readable tool name (e.g. "tr").
	Name string `json:"name"`
	// Probe is an argv used to obtain a stable version string.
	// If empty, the tool contributes no version input.
	Probe []string `json:"probe,omitempty"`
}

// Node is one build step.
type Node struct {
	// ID is the unique node identifier.
	ID string `json:"id"`
	// Command is executed with "sh -c". Working directory is Workdir,
	// stdin is /dev/null. Only commands explicitly declared in a
	// project specification are ever run.
	Command string `json:"command,omitempty"`
	// Inputs are workspace-relative regular files whose byte content
	// feeds the node's cache key.
	Inputs []string `json:"inputs,omitempty"`
	// Outputs are workspace-relative regular files the command creates.
	Outputs []string `json:"outputs,omitempty"`
	// DependsOn lists the IDs of dependency nodes.
	DependsOn []string `json:"depends_on,omitempty"`
	// Params are free-form scalar parameters feeding the cache key.
	Params map[string]string `json:"params,omitempty"`
	// Env is the allow-list of environment variables the node reads.
	// Each is captured from the server process environment and fed
	// into the cache key; only declared names are passed to the command.
	Env []string `json:"env,omitempty"`
	// Tools lists command line tools feeding the cache key through
	// their probed version strings.
	Tools []Tool `json:"tools,omitempty"`
}

// Graph is a collection of nodes.
type Graph struct {
	Nodes []*Node `json:"nodes"`
}

// Error is a graph-level validation error carrying structured detail.
type Error struct {
	Kind    string   `json:"kind"`
	NodeID  string   `json:"node,omitempty"`
	Cycle   []string `json:"cycle,omitempty"`
	Message string   `json:"message"`
}

func (e *Error) Error() string {
	if len(e.Cycle) > 0 {
		return fmt.Sprintf("%s: %s: %v", e.Kind, e.Message, e.Cycle)
	}
	if e.NodeID != "" {
		return fmt.Sprintf("%s: node %q: %s", e.Kind, e.NodeID, e.Message)
	}
	return fmt.Sprintf("%s: %s", e.Kind, e.Message)
}

func newError(kind, node, message string) *Error {
	return &Error{Kind: kind, NodeID: node, Message: message}
}

// byID returns a map of node ID -> node.
func (g *Graph) byID() (map[string]*Node, *Error) {
	m := make(map[string]*Node, len(g.Nodes))
	for _, n := range g.Nodes {
		if n == nil {
			return nil, newError("invalid_node", "", "nil node in graph")
		}
		if n.ID == "" {
			return nil, newError("invalid_node", "", "node has empty id")
		}
		if _, dup := m[n.ID]; dup {
			return nil, newError("duplicate_node", n.ID, "duplicate node id")
		}
		m[n.ID] = n
	}
	return m, nil
}

// Validate checks uniqueness, references, path safety and acyclicity.
// It returns the first error found.
func (g *Graph) Validate() *Error {
	byID, err := g.byID()
	if err != nil {
		return err
	}
	for _, n := range g.Nodes {
		for _, dep := range n.DependsOn {
			if _, ok := byID[dep]; !ok {
				return newError("unknown_dependency", n.ID, fmt.Sprintf("depends on unknown node %q", dep))
			}
			if dep == n.ID {
				return &Error{Kind: "cycle", NodeID: n.ID, Cycle: []string{n.ID},
					Message: "node depends on itself"}
			}
		}
		for _, p := range n.Inputs {
			if e := checkRelPath(n.ID, p, "input"); e != nil {
				return e
			}
		}
		for _, p := range n.Outputs {
			if e := checkRelPath(n.ID, p, "output"); e != nil {
				return e
			}
		}
	}
	if cyc := g.FindCycle(); cyc != nil {
		return &Error{Kind: "cycle", Cycle: cyc, Message: "dependency cycle detected"}
	}
	return nil
}

func checkRelPath(nodeID, p, kind string) *Error {
	if p == "" {
		return newError("invalid_path", nodeID, fmt.Sprintf("empty %s path", kind))
	}
	if path.IsAbs(p) {
		return newError("invalid_path", nodeID, fmt.Sprintf("%s path must be relative: %q", kind, p))
	}
	clean := path.Clean(p)
	if clean == ".." || strings.HasPrefix(clean, "../") {
		return newError("path_escape", nodeID, fmt.Sprintf("%s path escapes workspace: %q", kind, p))
	}
	if clean != p {
		return newError("invalid_path", nodeID, fmt.Sprintf("%s path must be clean (got %q, want %q)", kind, p, clean))
	}
	return nil
}

// FindCycle performs a DFS and returns one cycle as an ordered list of
// node IDs with the start node repeated at the end, or nil if acyclic.
func (g *Graph) FindCycle() []string {
	byID, err := g.byID()
	if err != nil {
		return nil
	}
	const (
		white = 0
		gray  = 1
		black = 2
	)
	color := make(map[string]int, len(byID))
	stack := []string{}

	// Deterministic iteration order.
	ids := make([]string, 0, len(byID))
	for id := range byID {
		ids = append(ids, id)
	}
	sort.Strings(ids)

	var dfs func(id string) []string
	dfs = func(id string) []string {
		color[id] = gray
		stack = append(stack, id)
		deps := append([]string(nil), byID[id].DependsOn...)
		sort.Strings(deps)
		for _, dep := range deps {
			if _, ok := byID[dep]; !ok {
				continue
			}
			switch color[dep] {
			case white:
				if cyc := dfs(dep); cyc != nil {
					return cyc
				}
			case gray:
				// Cycle: from first occurrence of dep in stack to end, plus dep.
				start := 0
				for i, s := range stack {
					if s == dep {
						start = i
						break
					}
				}
				cyc := append([]string(nil), stack[start:]...)
				return append(cyc, dep)
			}
		}
		stack = stack[:len(stack)-1]
		color[id] = black
		return nil
	}

	for _, id := range ids {
		if color[id] == white {
			if cyc := dfs(id); cyc != nil {
				return cyc
			}
		}
	}
	return nil
}

// Topo returns node IDs in dependency order (every dependency precedes
// its dependents). Tie-breaking is alphabetical. FindCycle is assumed to
// have returned nil.
func (g *Graph) Topo() ([]string, *Error) {
	byID, err := g.byID()
	if err != nil {
		return nil, err
	}
	indeg := make(map[string]int, len(byID))
	dependents := make(map[string][]string, len(byID))
	for _, n := range g.Nodes {
		indeg[n.ID] = len(uniqueSorted(n.DependsOn))
		for _, dep := range n.DependsOn {
			dependents[dep] = append(dependents[dep], n.ID)
		}
	}
	for k := range dependents {
		sort.Strings(dependents[k])
	}
	ready := make([]string, 0)
	for id, d := range indeg {
		if d == 0 {
			ready = append(ready, id)
		}
	}
	sort.Strings(ready)

	order := make([]string, 0, len(byID))
	for len(ready) > 0 {
		id := ready[0]
		ready = ready[1:]
		order = append(order, id)
		for _, dep := range dependents[id] {
			indeg[dep]--
			if indeg[dep] == 0 {
				ready = append(ready, dep)
			}
		}
		sort.Strings(ready)
	}
	if len(order) != len(byID) {
		if cyc := g.FindCycle(); cyc != nil {
			return nil, &Error{Kind: "cycle", Cycle: cyc, Message: "dependency cycle detected"}
		}
		return nil, newError("cycle", "", "topological sort failed")
	}
	return order, nil
}

// Closure returns the set containing targets and all of their transitive
// dependencies. Unknown target IDs produce an error.
func (g *Graph) Closure(targets []string) (map[string]bool, *Error) {
	byID, err := g.byID()
	if err != nil {
		return nil, err
	}
	set := map[string]bool{}
	var visit func(id string) *Error
	visit = func(id string) *Error {
		if set[id] {
			return nil
		}
		n, ok := byID[id]
		if !ok {
			return newError("unknown_target", id, fmt.Sprintf("target node %q does not exist", id))
		}
		set[id] = true
		for _, dep := range n.DependsOn {
			if e := visit(dep); e != nil {
				return e
			}
		}
		return nil
	}
	for _, t := range targets {
		if e := visit(t); e != nil {
			return nil, e
		}
	}
	return set, nil
}

// Node returns the node with the given ID.
func (g *Graph) Node(id string) *Node {
	for _, n := range g.Nodes {
		if n.ID == id {
			return n
		}
	}
	return nil
}

func uniqueSorted(in []string) []string {
	if len(in) == 0 {
		return in
	}
	out := append([]string(nil), in...)
	sort.Strings(out)
	w := 1
	for i := 1; i < len(out); i++ {
		if out[i] != out[i-1] {
			out[w] = out[i]
			w++
		}
	}
	return out[:w]
}
