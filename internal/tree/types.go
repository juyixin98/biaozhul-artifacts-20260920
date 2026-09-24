// Package tree defines the immutable behavior-tree definition model:
// node kinds, status propagation values, JSON parsing, validation and
// content hashing used to publish immutable versions.
package tree

// Status is the three-valued result a node returns from a tick,
// plus Running when a node is still in progress.
type Status string

const (
	Running Status = "running"
	Success Status = "success"
	Failure Status = "failure"
)

// IsTerminal reports whether the status is success or failure.
func (s Status) IsTerminal() bool { return s == Success || s == Failure }

// Kind enumerates the supported node types.
type Kind string

const (
	KindSequence Kind = "sequence"
	KindFallback Kind = "fallback"
	KindParallel Kind = "parallel"
	KindTimeout  Kind = "timeout"
	KindAction   Kind = "action"
)

// Node is one node of a tree definition.
type Node struct {
	ID       string   `json:"id"`
	Kind     Kind     `json:"kind"`
	Children []string `json:"children,omitempty"`
	// Action fields (kind == action).
	Action     string         `json:"action,omitempty"`
	Idempotent bool           `json:"idempotent,omitempty"`
	Params     map[string]any `json:"params,omitempty"`
	// Timeout field (kind == timeout): duration in milliseconds.
	TimeoutMS int64 `json:"timeout_ms,omitempty"`
	// Parallel policy: number of child successes / failures that decide the node.
	SuccessThreshold int `json:"success_threshold,omitempty"`
	FailureThreshold int `json:"failure_threshold,omitempty"`
}

// Definition is a complete tree: a root node id plus the node table.
type Definition struct {
	Root  string          `json:"root"`
	Nodes map[string]Node `json:"nodes"`
}

// Clone returns a deep copy safe for the caller to mutate.
func (d *Definition) Clone() *Definition {
	if d == nil {
		return nil
	}
	out := &Definition{Root: d.Root, Nodes: make(map[string]Node, len(d.Nodes))}
	for id, n := range d.Nodes {
		// n is a value copy from the map. Replace its header fields before
		// inserting it; assigning into n.Params and then storing n would not
		// update the copy held by out.Nodes.
		n.Children = append([]string(nil), n.Children...)
		if n.Params != nil {
			n.Params = cloneParams(n.Params)
		}
		out.Nodes[id] = n
	}
	return out
}

func cloneParams(in map[string]any) map[string]any {
	out := make(map[string]any, len(in))
	for k, v := range in {
		switch t := v.(type) {
		case map[string]any:
			out[k] = cloneParams(t)
		case []any:
			cp := make([]any, len(t))
			for i, e := range t {
				if m, ok := e.(map[string]any); ok {
					cp[i] = cloneParams(m)
				} else {
					cp[i] = e
				}
			}
			out[k] = cp
		default:
			out[k] = v
		}
	}
	return out
}
