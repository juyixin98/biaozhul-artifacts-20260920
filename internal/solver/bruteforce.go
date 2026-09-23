package solver

import (
	"sort"

	"depresolve/internal/semver"
)

// BruteForceSolve is the exhaustive reference implementation used by tests
// to cross-check the backtracking solver on small graphs. It enumerates
// every combination of versions over every reachable package, keeps the
// valid ones (all constraints satisfied, selected set exactly the packages
// required from the roots), and picks the optimum under the same
// preference the solver's depth-first search implements: simulate the
// solver's traversal (smallest unchosen required package first) over each
// valid combination, and rank combinations by the resulting choice
// sequence — at the first position where two sequences differ, the higher
// version wins. The solver, trying candidates in descending order, finds
// exactly this maximum.
//
// It is exponential and must only be used on small fixtures.
func BruteForceSolve(reg Registry, roots map[string]string) (map[string]string, bool) {
	s, err := NewSolver(reg, roots)
	if err != nil {
		return nil, false
	}
	// Reachable packages: closure of the whole registry graph from roots.
	reachable := map[string]bool{}
	var visit func(name string)
	visit = func(name string) {
		if reachable[name] {
			return
		}
		reachable[name] = true
		for _, pv := range s.parsed[name] {
			for dep := range pv.deps {
				visit(dep)
			}
		}
	}
	rootNames := make([]string, 0, len(roots))
	for name := range roots {
		rootNames = append(rootNames, name)
		visit(name)
	}
	sort.Strings(rootNames)

	names := make([]string, 0, len(reachable))
	for name := range reachable {
		names = append(names, name)
	}
	sort.Strings(names)

	lists := make([][]parsedVersion, len(names))
	for i, name := range names {
		lists[i] = s.parsed[name]
	}

	var best map[string]string
	var bestKey []choice

	// Odometer enumeration; idx[i] == -1 means "package not selected".
	idx := make([]int, len(names))
	for i := range idx {
		idx[i] = -1
	}
	for {
		if sol, key, ok := evaluate(s, names, lists, idx, rootNames); ok {
			if best == nil || keyGreater(key, bestKey) {
				best, bestKey = sol, key
			}
		}
		p := 0
		for ; p < len(names); p++ {
			if idx[p] < len(lists[p])-1 {
				idx[p]++
				break
			}
			idx[p] = -1
		}
		if p == len(names) {
			break
		}
	}
	if best == nil {
		return nil, false
	}
	return best, true
}

// choice is one step of a combination's simulated traversal.
type choice struct {
	name string
	ver  semver.Version
}

// evaluate checks one full combination. A combination is valid when:
//   - every root is selected and satisfies its root constraint;
//   - every selected version's deps are selected and satisfy their
//     constraints;
//   - every selected package is reachable from a root through selected
//     packages (no spurious selections), so the selected set is exactly
//     the required closure.
//
// For valid combinations it returns the solution map and the traversal
// key used for preference comparison.
func evaluate(s *Solver, names []string, lists [][]parsedVersion, idx []int, rootNames []string) (map[string]string, []choice, bool) {
	selected := map[string]parsedVersion{}
	for i, name := range names {
		if idx[i] >= 0 {
			selected[name] = lists[i][idx[i]]
		}
	}
	for _, name := range rootNames {
		pv, ok := selected[name]
		if !ok || !s.roots[name].Allows(pv.ver) {
			return nil, nil, false
		}
	}
	for _, pv := range selected {
		for dep, c := range pv.deps {
			dpv, ok := selected[dep]
			if !ok || !c.Allows(dpv.ver) {
				return nil, nil, false
			}
		}
	}
	// Reachability from roots over selected packages.
	seen := map[string]bool{}
	queue := append([]string(nil), rootNames...)
	for len(queue) > 0 {
		name := queue[0]
		queue = queue[1:]
		if seen[name] {
			continue
		}
		seen[name] = true
		for dep := range selected[name].deps {
			queue = append(queue, dep)
		}
	}
	for name := range selected {
		if !seen[name] {
			return nil, nil, false
		}
	}
	sol := make(map[string]string, len(selected))
	for name, pv := range selected {
		sol[name] = pv.ver.String()
	}
	return sol, traversalKey(s, rootNames, selected), true
}

// traversalKey replays the solver's package-selection order over a valid
// combination: repeatedly take the alphabetically smallest package with
// pending requirements, record the version the combination assigns to it,
// and impose that version's dependency constraints.
func traversalKey(s *Solver, rootNames []string, selected map[string]parsedVersion) []choice {
	reqs := map[string][]*semver.Constraint{}
	add := func(name string, c *semver.Constraint) {
		reqs[name] = append(reqs[name], c)
	}
	for _, name := range rootNames {
		add(name, s.roots[name])
	}
	chosen := map[string]bool{}
	var key []choice
	for {
		name := ""
		for n := range reqs {
			if !chosen[n] && (name == "" || n < name) {
				name = n
			}
		}
		if name == "" {
			return key
		}
		pv, ok := selected[name]
		if !ok {
			// Required but not selected: invalid combinations are filtered
			// before keys are computed, so this is unreachable.
			return key
		}
		chosen[name] = true
		key = append(key, choice{name: name, ver: pv.ver})
		depNames := make([]string, 0, len(pv.deps))
		for dep := range pv.deps {
			depNames = append(depNames, dep)
		}
		sort.Strings(depNames)
		for _, dep := range depNames {
			add(dep, pv.deps[dep])
		}
	}
}

// keyGreater reports whether choice sequence a is preferred over b. Two
// valid combinations sharing a prefix of choices share the same search
// state, so their keys diverge at a position naming the same package with
// a different version; the higher version wins.
func keyGreater(a, b []choice) bool {
	for i := 0; i < len(a) && i < len(b); i++ {
		if a[i].name != b[i].name {
			// Should not happen for valid combinations; rank by name to
			// stay total and deterministic.
			return a[i].name < b[i].name
		}
		if c := semver.Compare(a[i].ver, b[i].ver); c != 0 {
			return c > 0
		}
	}
	return len(a) > len(b)
}
