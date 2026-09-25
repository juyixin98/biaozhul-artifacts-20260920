package scheduler

import (
	"fmt"
	"sort"
)

// validateAndIndex checks the static spec and returns nodes keyed by name
// plus each node's reverse dependents. It also performs cycle detection.
func validateAndIndex(spec *DagSpec) (map[string]*NodeSpec, map[string][]string, error) {
	if spec == nil {
		return nil, nil, fmt.Errorf("dag spec is nil")
	}
	if len(spec.Nodes) == 0 {
		return nil, nil, fmt.Errorf("dag has no nodes")
	}
	nodes := make(map[string]*NodeSpec, len(spec.Nodes))
	for i := range spec.Nodes {
		n := &spec.Nodes[i]
		if n.Name == "" {
			return nil, nil, fmt.Errorf("node at index %d has empty name", i)
		}
		if _, dup := nodes[n.Name]; dup {
			return nil, nil, fmt.Errorf("duplicate node name %q", n.Name)
		}
		switch n.Policy {
		case "", RequireAllSuccess, RequireAllEnd:
		default:
			return nil, nil, fmt.Errorf("node %q has invalid policy %q", n.Name, n.Policy)
		}
		if n.MaxAttempts < 0 {
			return nil, nil, fmt.Errorf("node %q has negative maxAttempts", n.Name)
		}
		nodes[n.Name] = n
	}
	for name, n := range nodes {
		seen := make(map[string]bool, len(n.Deps))
		for _, d := range n.Deps {
			if d == name {
				return nil, nil, fmt.Errorf("node %q depends on itself", name)
			}
			if _, ok := nodes[d]; !ok {
				return nil, nil, fmt.Errorf("node %q depends on unknown node %q", name, d)
			}
			if seen[d] {
				return nil, nil, fmt.Errorf("node %q lists duplicate dependency %q", name, d)
			}
			seen[d] = true
		}
	}
	if cycle := findCycle(nodes); cycle != nil {
		return nil, nil, fmt.Errorf("dag contains a cycle: %v", cycle)
	}
	dependents := make(map[string][]string, len(nodes))
	for name, n := range nodes {
		for _, d := range n.Deps {
			dependents[d] = append(dependents[d], name)
		}
	}
	return nodes, dependents, nil
}

// findCycle performs iterative DFS with white/gray/black coloring and
// returns the names forming one cycle, or nil when the graph is acyclic.
func findCycle(nodes map[string]*NodeSpec) []string {
	const (
		white = 0
		gray  = 1
		black = 2
	)
	color := make(map[string]int, len(nodes))
	// Deterministic traversal order.
	names := make([]string, 0, len(nodes))
	for name := range nodes {
		names = append(names, name)
	}
	sort.Strings(names)

	type frame struct {
		name string
		i    int
	}
	pathIndex := make(map[string]int)
	var stack []*frame
	push := func(name string) {
		color[name] = gray
		pathIndex[name] = len(stack)
		stack = append(stack, &frame{name: name})
	}
	pop := func() {
		top := stack[len(stack)-1]
		color[top.name] = black
		delete(pathIndex, top.name)
		stack = stack[:len(stack)-1]
	}
	for _, root := range names {
		if color[root] != white {
			continue
		}
		push(root)
		for len(stack) > 0 {
			top := stack[len(stack)-1]
			node := nodes[top.name]
			if top.i < len(node.Deps) {
				next := node.Deps[top.i]
				top.i++
				switch color[next] {
				case gray:
					// Back edge: extract cycle from the DFS stack.
					start := pathIndex[next]
					cycle := make([]string, 0, len(stack)-start+1)
					for _, f := range stack[start:] {
						cycle = append(cycle, f.name)
					}
					cycle = append(cycle, next)
					return cycle
				case white:
					push(next)
				}
			} else {
				pop()
			}
		}
	}
	return nil
}
