package scheduler

import (
	"errors"
	"fmt"
	"strings"
	"time"
)

// NodeSpec declares one DAG node.
type NodeSpec struct {
	// ID must be unique within the DAG and non-empty.
	ID string `json:"id"`
	// TaskType selects an executor from the engine's registry.
	TaskType string `json:"task_type"`
	// Params are passed verbatim to the executor every attempt.
	Params map[string]string `json:"params,omitempty"`
	// DependsOn lists node ids that must be resolved before this node.
	// Duplicate entries are de-duplicated.
	DependsOn []string `json:"depends_on,omitempty"`
	// Policy controls how non-success dependency outcomes are treated.
	// Defaults to all_success.
	Policy DependencyPolicy `json:"policy,omitempty"`
	// MaxAttempts is the total attempt budget (1 = no retry). Must be >= 1.
	MaxAttempts int `json:"max_attempts,omitempty"`
	// Backoff is the wait between a failed attempt and the next one.
	// The engine clock measures it, so a fake clock makes retries instant.
	Backoff Duration `json:"backoff,omitempty"`
}

// Duration wraps time.Duration with JSON (un)marshalling in human units
// ("500ms", "2s") as well as raw nanosecond integers.
type Duration struct {
	time.Duration
}

func (d Duration) MarshalJSON() ([]byte, error) {
	return []byte(`"` + d.Duration.String() + `"`), nil
}

func (d *Duration) UnmarshalJSON(b []byte) error {
	s := strings.Trim(string(b), `"`)
	if s == "" || s == "null" {
		d.Duration = 0
		return nil
	}
	dur, err := time.ParseDuration(s)
	if err != nil {
		return err
	}
	d.Duration = dur
	return nil
}

// DAG is a submitted job graph.
type DAG struct {
	ID    string     `json:"id,omitempty"`
	Nodes []NodeSpec `json:"nodes"`
}

func (d *DAG) normalize() error {
	seen := make(map[string]bool, len(d.Nodes))
	for i := range d.Nodes {
		n := &d.Nodes[i]
		n.ID = strings.TrimSpace(n.ID)
		if n.ID == "" {
			return errors.New("node with empty id")
		}
		if seen[n.ID] {
			return fmt.Errorf("duplicate node id %q", n.ID)
		}
		seen[n.ID] = true
		if n.TaskType == "" {
			return fmt.Errorf("node %q: task_type is required", n.ID)
		}
		if n.Policy == "" {
			n.Policy = PolicyAllSuccess
		}
		if n.Policy != PolicyAllSuccess && n.Policy != PolicyAllFinished {
			return fmt.Errorf("node %q: unknown dependency policy %q", n.ID, n.Policy)
		}
		if n.MaxAttempts == 0 {
			n.MaxAttempts = 1
		}
		if n.MaxAttempts < 1 {
			return fmt.Errorf("node %q: max_attempts must be >= 1", n.ID)
		}
		if n.Backoff.Duration < 0 {
			return fmt.Errorf("node %q: backoff must be >= 0", n.ID)
		}
	}
	// Validate and de-duplicate dependency lists.
	for i := range d.Nodes {
		n := &d.Nodes[i]
		if len(n.DependsOn) == 0 {
			continue
		}
		uniq := make([]string, 0, len(n.DependsOn))
		depSeen := make(map[string]bool, len(n.DependsOn))
		for _, dep := range n.DependsOn {
			if dep == n.ID {
				return fmt.Errorf("node %q depends on itself", n.ID)
			}
			if !seen[dep] {
				return fmt.Errorf("node %q depends on unknown node %q", n.ID, dep)
			}
			if depSeen[dep] {
				continue
			}
			depSeen[dep] = true
			uniq = append(uniq, dep)
		}
		n.DependsOn = uniq
	}
	return nil
}

// Validate normalizes defaults and verifies structural integrity, including
// cycle detection. On a cycle it returns an error whose message lists the
// nodes forming the cycle.
func (d *DAG) Validate() error {
	if len(d.Nodes) == 0 {
		return errors.New("dag has no nodes")
	}
	if err := d.normalize(); err != nil {
		return err
	}
	if cycle := findCycle(d.Nodes); cycle != nil {
		return fmt.Errorf("dependency cycle detected: %s", strings.Join(cycle, " -> "))
	}
	return nil
}

// findCycle returns the node ids of one directed cycle as a closed path
// (first node repeated at the end), or nil if acyclic. Iterative DFS with
// WHITE/GRAY/BLACK coloring; parent pointers reconstruct the cycle path.
// Traversal direction is node -> its dependencies.
func findCycle(nodes []NodeSpec) []string {
	const white, gray, black = 0, 1, 2
	color := make(map[string]int, len(nodes))
	adj := make(map[string][]string, len(nodes))
	for _, n := range nodes {
		adj[n.ID] = n.DependsOn
	}
	order := make([]string, 0, len(nodes))
	for _, n := range nodes {
		order = append(order, n.ID)
	}

	type frame struct {
		id  string
		idx int
	}
	// parent[v] is the node from which v was first reached.
	parent := map[string]string{}
	for _, root := range order {
		if color[root] != white {
			continue
		}
		color[root] = gray
		stack := []frame{{id: root}}
		for len(stack) > 0 {
			top := &stack[len(stack)-1]
			if top.idx >= len(adj[top.id]) {
				color[top.id] = black
				stack = stack[:len(stack)-1]
				continue
			}
			next := adj[top.id][top.idx]
			top.idx++
			switch color[next] {
			case white:
				parent[next] = top.id
				color[next] = gray
				stack = append(stack, frame{id: next})
			case gray:
				// Back edge top.id -> next, where next is an ancestor on
				// the current DFS stack. Walk parent links from top.id up
				// to next, then reverse and close the loop.
				chain := []string{top.id}
				for chain[len(chain)-1] != next {
					chain = append(chain, parent[chain[len(chain)-1]])
				}
				for i, j := 0, len(chain)-1; i < j; i, j = i+1, j-1 {
					chain[i], chain[j] = chain[j], chain[i]
				}
				return append(chain, next)
			}
		}
	}
	return nil
}
