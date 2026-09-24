package tree

import (
	"fmt"
	"regexp"
	"sort"
)

var idPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`)

// IsKnownAction is implemented by the local stub registry. It is wired in by
// the caller so that the tree package stays free of execution dependencies.
type IsKnownAction func(name string) bool

// Validate checks structural integrity of a definition. knownActions may be
// nil, in which case every non-empty action name is accepted (used before the
// registry is wired in tests of pure structure).
func (d *Definition) Validate(knownActions IsKnownAction) error {
	if d == nil {
		return fmt.Errorf("definition is required")
	}
	if len(d.Nodes) == 0 {
		return fmt.Errorf("nodes must not be empty")
	}
	if !idPattern.MatchString(d.Root) {
		return fmt.Errorf("root id %q is invalid", d.Root)
	}
	if _, ok := d.Nodes[d.Root]; !ok {
		return fmt.Errorf("root node %q not found in nodes", d.Root)
	}

	seen := make(map[string]bool, len(d.Nodes))
	for id, n := range d.Nodes {
		if !idPattern.MatchString(id) {
			return fmt.Errorf("node id %q is invalid", id)
		}
		if n.ID != "" && n.ID != id {
			return fmt.Errorf("node %q carries mismatched embedded id %q", id, n.ID)
		}
		if seen[id] {
			return fmt.Errorf("duplicate node id %q", id)
		}
		seen[id] = true

		switch n.Kind {
		case KindSequence, KindFallback:
			if len(n.Children) == 0 {
				return fmt.Errorf("node %q: %s requires at least one child", id, n.Kind)
			}
		case KindParallel:
			if len(n.Children) == 0 {
				return fmt.Errorf("node %q: parallel requires at least one child", id)
			}
			s := n.SuccessThreshold
			f := n.FailureThreshold
			if s <= 0 || f <= 0 {
				return fmt.Errorf("node %q: parallel success/failure thresholds must be > 0", id)
			}
			if s > len(n.Children) || f > len(n.Children) {
				return fmt.Errorf("node %q: thresholds must not exceed child count %d", id, len(n.Children))
			}
			// Success/failure quotas may overlap by at most one child
			// (s+f <= n+1, the same bound BehaviorTree.CPP enforces): when
			// s+f == n+1 the quotas cannot both be reached, since that would
			// need s+f distinct terminal children but only n exist. The
			// s+f <= n case means both quotas can be reached in one tick;
			// the engine resolves it deterministically with success priority
			// (success threshold evaluated first).
			if s+f > len(n.Children)+1 {
				return fmt.Errorf("node %q: success_threshold(%d)+failure_threshold(%d) must be <= children(%d)+1",
					id, s, f, len(n.Children))
			}
		case KindTimeout:
			if len(n.Children) != 1 {
				return fmt.Errorf("node %q: timeout requires exactly one child", id)
			}
			if n.TimeoutMS <= 0 {
				return fmt.Errorf("node %q: timeout_ms must be > 0", id)
			}
		case KindAction:
			if len(n.Children) != 0 {
				return fmt.Errorf("node %q: action must not have children", id)
			}
			if n.Action == "" {
				return fmt.Errorf("node %q: action name is required", id)
			}
			if knownActions != nil && !knownActions(n.Action) {
				return fmt.Errorf("node %q: unknown action %q", id, n.Action)
			}
		default:
			return fmt.Errorf("node %q: unknown kind %q", id, n.Kind)
		}

		// Local duplicates within the child list.
		dup := make(map[string]bool, len(n.Children))
		for _, c := range n.Children {
			if dup[c] {
				return fmt.Errorf("node %q lists child %q more than once", id, c)
			}
			dup[c] = true
			if _, ok := d.Nodes[c]; !ok {
				return fmt.Errorf("node %q references missing child %q", id, c)
			}
		}
	}

	// Every node must be reachable exactly once from the root (a tree, not a DAG).
	visited := make(map[string]bool, len(d.Nodes))
	var walk func(id string) error
	walk = func(id string) error {
		if visited[id] {
			return fmt.Errorf("node %q is reachable more than once (definitions must be trees)", id)
		}
		visited[id] = true
		for _, c := range d.Nodes[id].Children {
			if err := walk(c); err != nil {
				return err
			}
		}
		return nil
	}
	if err := walk(d.Root); err != nil {
		return err
	}
	if len(visited) != len(d.Nodes) {
		orphans := make([]string, 0)
		for id := range d.Nodes {
			if !visited[id] {
				orphans = append(orphans, id)
			}
		}
		sort.Strings(orphans)
		return fmt.Errorf("unreachable nodes: %v", orphans)
	}
	return nil
}
