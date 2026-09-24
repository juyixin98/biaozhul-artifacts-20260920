package engine

import (
	"fmt"
	"regexp"

	"dagexec/internal/model"
	"dagexec/internal/task"
)

const (
	maxNodes = 256
	maxDeps  = 64
)

var idRe = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

// validateSpec checks everything that can be known before a run starts:
// node IDs, duplicates, unknown/missing dependencies, cycles, unknown task
// types and retry bounds. It returns a descriptive error.
func validateSpec(d model.DAG, reg *task.Registry) error {
	if len(d.Nodes) == 0 {
		return fmt.Errorf("dag has no nodes")
	}
	if len(d.Nodes) > maxNodes {
		return fmt.Errorf("too many nodes: %d > %d", len(d.Nodes), maxNodes)
	}
	if d.Retries < 0 || d.Retries > 100 {
		return fmt.Errorf("dag retries must be in [0,100], got %d", d.Retries)
	}
	if d.BackoffMs < 0 || d.BackoffMs > 3_600_000 {
		return fmt.Errorf("dag backoff_ms must be in [0,3600000], got %d", d.BackoffMs)
	}

	nodes := make(map[string]*model.Node, len(d.Nodes))
	for i := range d.Nodes {
		n := &d.Nodes[i]
		if !idRe.MatchString(n.ID) {
			return fmt.Errorf("node %q: id must match %s", n.ID, idRe.String())
		}
		if _, dup := nodes[n.ID]; dup {
			return fmt.Errorf("duplicate node id %q", n.ID)
		}
		if _, ok := reg.Get(n.Type); !ok {
			return fmt.Errorf("node %q: unknown task type %q", n.ID, n.Type)
		}
		if len(n.Deps) > maxDeps {
			return fmt.Errorf("node %q: too many dependencies: %d > %d", n.ID, len(n.Deps), maxDeps)
		}
		if n.Retries != nil && (*n.Retries < 0 || *n.Retries > 100) {
			return fmt.Errorf("node %q: retries must be in [0,100]", n.ID)
		}
		if n.BackoffMs != nil && (*n.BackoffMs < 0 || *n.BackoffMs > 3_600_000) {
			return fmt.Errorf("node %q: backoff_ms must be in [0,3600000]", n.ID)
		}
		for _, dep := range n.Deps {
			if dep == n.ID {
				return fmt.Errorf("node %q depends on itself", n.ID)
			}
		}
		nodes[n.ID] = n
	}

	for _, n := range d.Nodes {
		seen := make(map[string]bool, len(n.Deps))
		for _, dep := range n.Deps {
			if seen[dep] {
				return fmt.Errorf("node %q: duplicate dependency %q", n.ID, dep)
			}
			seen[dep] = true
			if _, ok := nodes[dep]; !ok {
				return fmt.Errorf("node %q: missing dependency %q", n.ID, dep)
			}
		}
	}

	if cycle := findCycle(d.Nodes); cycle != nil {
		return fmt.Errorf("dependency cycle detected: %v", cycle)
	}
	return nil
}

// findCycle runs an iterative DFS coloring and returns one offending node
// chain (node names forming a cycle, first node repeated at the end).
// White=0, gray=1, black=2.
func findCycle(nodes []model.Node) []string {
	adj := make(map[string][]string, len(nodes))
	for _, n := range nodes {
		adj[n.ID] = n.Deps
	}
	color := make(map[string]int, len(nodes))
	var stack []string

	var dfs func(u string) []string
	dfs = func(u string) []string {
		color[u] = 1
		stack = append(stack, u)
		for _, v := range adj[u] {
			switch color[v] {
			case 0:
				if cyc := dfs(v); cyc != nil {
					return cyc
				}
			case 1:
				// Back edge u->v: extract cycle from first occurrence of v.
				for i, x := range stack {
					if x == v {
						path := append([]string{}, stack[i:]...)
						return append(path, v)
					}
				}
			}
		}
		stack = stack[:len(stack)-1]
		color[u] = 2
		return nil
	}

	for _, n := range nodes {
		if color[n.ID] == 0 {
			if cyc := dfs(n.ID); cyc != nil {
				return cyc
			}
		}
	}
	return nil
}
