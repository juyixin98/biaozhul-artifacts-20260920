// Package dag implements the domain model of the resumable DAG executor:
// task specifications, node run-state, validation (missing deps / cycles)
// and the whitelist of pure built-in task functions.
package dag

import (
	"fmt"
	"strings"
	"time"
)

// NodeSpec is the immutable declaration of one DAG node.
type NodeSpec struct {
	ID          string                 `json:"id"`
	Task        string                 `json:"task"`
	Params      map[string]interface{} `json:"params,omitempty"`
	Deps        []string               `json:"deps,omitempty"`
	MaxAttempts int                    `json:"max_attempts,omitempty"` // per activation; <=0 falls back to DAG default
}

// Spec is a submitted DAG definition.
type Spec struct {
	Name        string     `json:"name,omitempty"`
	Nodes       []NodeSpec `json:"nodes"`
	MaxAttempts int        `json:"max_attempts,omitempty"` // DAG-wide default per activation
	MaxParallel int        `json:"max_parallel,omitempty"` // 0 => scheduler default
}

// Lifecycle statuses.
const (
	StatusPending   = "pending"   // never launched
	StatusRunning   = "running"   // executing an attempt
	StatusSuccess   = "success"   // completed with a cached result
	StatusFailed    = "failed"    // latest attempt failed, may still retry
	StatusBlocked   = "blocked"   // an upstream failed permanently
	StatusCancelled = "cancelled" // cancelled by the user before/while running
)

// DAG-level statuses.
const (
	DAGPending    = "pending"    // accepted, not (yet) running
	DAGRunning    = "running"    // at least one node active / retry pending
	DAGSucceeded  = "succeeded"  // every node succeeded
	DAGFailed     = "failed"     // a node exhausted retries; downstreams blocked
	DAGCancelling = "cancelling" // cancel requested; waiting for active tasks
	DAGCancelled  = "cancelled"  // cancel fully applied
)

// NodeState is the persisted run-time state of one node.
type NodeState struct {
	Status     string      `json:"status"`
	Attempt    int         `json:"attempt"`    // attempts in the current activation
	TotalRuns  int         `json:"total_runs"` // attempts over the whole lifetime (never reset)
	Result     interface{} `json:"result,omitempty"`
	Error      string      `json:"error,omitempty"`
	RetryAfter *time.Time  `json:"retry_after,omitempty"`
	StartedAt  *time.Time  `json:"started_at,omitempty"`
	FinishedAt *time.Time  `json:"finished_at,omitempty"`
}

// DAGState is the full persisted state of one DAG instance.
type DAGState struct {
	ID        string                `json:"id"`
	Name      string                `json:"name,omitempty"`
	Spec      Spec                  `json:"spec"`
	Nodes     map[string]*NodeState `json:"nodes"`
	Status    string                `json:"status"`
	CreatedAt time.Time             `json:"created_at"`
	UpdatedAt time.Time             `json:"updated_at"`
	FailNode  string                `json:"fail_node,omitempty"` // first node that exhausted retries
}

// HasNode reports whether the spec contains a node with the given id.
func (s *Spec) HasNode(id string) bool {
	for i := range s.Nodes {
		if s.Nodes[i].ID == id {
			return true
		}
	}
	return false
}

// MaxAttemptsFor resolves the per-activation attempt budget for a node.
func (s *Spec) MaxAttemptsFor(n NodeSpec, fallback int) int {
	if n.MaxAttempts > 0 {
		return n.MaxAttempts
	}
	if s.MaxAttempts > 0 {
		return s.MaxAttempts
	}
	return fallback
}

// MaxParallelFor resolves the concurrency limit for a DAG instance.
func (s *Spec) MaxParallelFor(fallback int) int {
	if s.MaxParallel > 0 {
		return s.MaxParallel
	}
	return fallback
}

// ValidationError lists every problem found in a submitted spec so the API
// can return all of them at once (bad request, not a server error).
type ValidationError struct {
	Errors []string
}

func (e *ValidationError) Error() string {
	return "invalid DAG spec: " + strings.Join(e.Errors, "; ")
}

// Validate checks: unique non-empty ids, whitelisted task names, existing
// non-duplicate deps, at least one node, and absence of dependency cycles.
// Self-loops count as cycles.
func (s *Spec) Validate(whitelist map[string]TaskFunc) error {
	var errs []string
	if len(s.Nodes) == 0 {
		errs = append(errs, "nodes must not be empty")
	}

	seen := map[string]bool{}
	deps := map[string][]string{}
	for i := range s.Nodes {
		n := &s.Nodes[i]
		if strings.TrimSpace(n.ID) == "" {
			errs = append(errs, fmt.Sprintf("node[%d] has empty id", i))
			continue
		}
		if seen[n.ID] {
			errs = append(errs, fmt.Sprintf("duplicate node id %q", n.ID))
		}
		seen[n.ID] = true
		if _, ok := whitelist[n.Task]; !ok {
			errs = append(errs, fmt.Sprintf("node %q: task %q is not in the whitelist", n.ID, n.Task))
		}
		depSeen := map[string]bool{}
		for _, d := range n.Deps {
			if depSeen[d] {
				errs = append(errs, fmt.Sprintf("node %q: duplicate dependency %q", n.ID, d))
			}
			depSeen[d] = true
		}
		deps[n.ID] = n.Deps
	}

	// Missing dependencies.
	for id, ds := range deps {
		for _, d := range ds {
			if !seen[d] {
				errs = append(errs, fmt.Sprintf("node %q depends on missing node %q", id, d))
			}
		}
	}

	// Cycle detection via Kahn's topological sort; remaining nodes sit on
	// (or downstream of) a cycle. Only run when ids are well-formed.
	if len(errs) == 0 || missingDepsOnly(errs) {
		indeg := map[string]int{}
		adj := map[string][]string{}
		for id, ds := range deps {
			indeg[id] += 0
			for _, d := range ds {
				if seen[d] {
					adj[d] = append(adj[d], id)
					indeg[id]++
				}
			}
		}
		var queue []string
		for id, deg := range indeg {
			if deg == 0 {
				queue = append(queue, id)
			}
		}
		visited := 0
		for len(queue) > 0 {
			cur := queue[0]
			queue = queue[1:]
			visited++
			for _, next := range adj[cur] {
				indeg[next]--
				if indeg[next] == 0 {
					queue = append(queue, next)
				}
			}
		}
		if visited != len(indeg) {
			var cyclic []string
			for id, deg := range indeg {
				if deg > 0 {
					cyclic = append(cyclic, id)
				}
			}
			errs = append(errs, fmt.Sprintf("dependency cycle detected involving nodes: %s",
				strings.Join(cyclic, ", ")))
		}
	}

	if len(errs) > 0 {
		return &ValidationError{Errors: errs}
	}
	return nil
}

func missingDepsOnly(errs []string) bool {
	for _, e := range errs {
		if !strings.Contains(e, "depends on missing node") &&
			!strings.Contains(e, "empty id") &&
			!strings.Contains(e, "duplicate") &&
			!strings.Contains(e, "not in the whitelist") {
			return false
		}
	}
	return true
}
