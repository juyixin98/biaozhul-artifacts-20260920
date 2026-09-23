// Package solver resolves a deterministic set of package versions that
// satisfies all dependency constraints, using backtracking search over a
// locally supplied registry. It never touches the network.
package solver

import (
	"fmt"
	"sort"

	"depresolve/internal/semver"
)

// VersionInfo describes one available version of a package.
type VersionInfo struct {
	Version string            `json:"version"`
	Deps    map[string]string `json:"deps,omitempty"` // dep name -> constraint
}

// Registry maps package name -> available versions.
type Registry map[string][]VersionInfo

// Requirement records who asked for a package with which constraint.
type Requirement struct {
	// Requirer is "" for root requirements.
	Requirer   string `json:"requirer"`
	Constraint string `json:"constraint"`
}

// Decision is one entry in the search trace.
type Decision struct {
	Seq     int    `json:"seq"`
	Depth   int    `json:"depth"`
	Kind    string `json:"kind"` // "select", "try", "reject", "conflict", "backtrack", "cycle", "solution"
	Package string `json:"package,omitempty"`
	Version string `json:"version,omitempty"`
	Reason  string `json:"reason"`
}

// Conflict explains why no solution exists.
type Conflict struct {
	Package string   `json:"package"`
	Reason  string   `json:"reason"`
	Chains  []string `json:"chains"` // human-readable dependency chains to the conflict
}

// Result is the outcome of a Solve call.
type Result struct {
	OK       bool              `json:"ok"`
	Solution map[string]string `json:"solution,omitempty"`
	Conflict *Conflict         `json:"conflict,omitempty"`
	Log      []Decision        `json:"log"`
}

type parsedVersion struct {
	ver  semver.Version
	info VersionInfo
	deps map[string]*semver.Constraint // parsed, sorted by dep name
}

// Solver holds the prepared registry and search state.
type Solver struct {
	reg    Registry
	parsed map[string][]parsedVersion // versions sorted descending
	roots  map[string]*semver.Constraint
	log    []Decision
	// lastConflict is the deepest assignment at which the search hit a
	// dead end; used to build the conflict explanation.
	lastConflict      *assignment
	lastConflictDepth int
	lastConflictPkg   string
}

// NewSolver validates the registry and root requirements.
func NewSolver(reg Registry, roots map[string]string) (*Solver, error) {
	s := &Solver{
		reg:    reg,
		parsed: map[string][]parsedVersion{},
		roots:  map[string]*semver.Constraint{},
	}
	for name, cstr := range roots {
		c, err := semver.ParseConstraint(cstr)
		if err != nil {
			return nil, fmt.Errorf("root requirement %s: %w", name, err)
		}
		s.roots[name] = c
	}
	for name, versions := range reg {
		if len(versions) == 0 {
			return nil, fmt.Errorf("registry: package %q has no versions", name)
		}
		seen := map[string]bool{}
		var pvs []parsedVersion
		for _, vi := range versions {
			v, err := semver.Parse(vi.Version)
			if err != nil {
				return nil, fmt.Errorf("registry: package %q: %w", name, err)
			}
			if seen[v.String()] {
				return nil, fmt.Errorf("registry: package %q lists %s twice", name, v)
			}
			seen[v.String()] = true
			pv := parsedVersion{ver: v, info: vi, deps: map[string]*semver.Constraint{}}
			for dep, cstr := range vi.Deps {
				if _, ok := reg[dep]; !ok {
					return nil, fmt.Errorf("registry: %s@%s depends on unknown package %q", name, v, dep)
				}
				c, err := semver.ParseConstraint(cstr)
				if err != nil {
					return nil, fmt.Errorf("registry: %s@%s dep %s: %w", name, v, dep, err)
				}
				pv.deps[dep] = c
			}
			pvs = append(pvs, pv)
		}
		// Sort descending: solver tries highest versions first.
		sort.Slice(pvs, func(i, j int) bool {
			return semver.Compare(pvs[i].ver, pvs[j].ver) > 0
		})
		s.parsed[name] = pvs
	}
	for name := range roots {
		if _, ok := reg[name]; !ok {
			return nil, fmt.Errorf("root requirement %q is not in the registry", name)
		}
	}
	return s, nil
}

func (s *Solver) emit(depth int, kind, pkg, ver, reason string) {
	s.log = append(s.log, Decision{
		Seq:     len(s.log) + 1,
		Depth:   depth,
		Kind:    kind,
		Package: pkg,
		Version: ver,
		Reason:  reason,
	})
}

// assignment is the current partial solution.
type assignment struct {
	chosen map[string]parsedVersion
	// reqs[name] lists every requirement imposed on name so far.
	reqs map[string][]Requirement
}

func (a *assignment) clone() *assignment {
	b := &assignment{
		chosen: make(map[string]parsedVersion, len(a.chosen)),
		reqs:   make(map[string][]Requirement, len(a.reqs)),
	}
	for k, v := range a.chosen {
		b.chosen[k] = v
	}
	for k, v := range a.reqs {
		b.reqs[k] = append([]Requirement(nil), v...)
	}
	return b
}

// Solve runs the backtracking search and returns the deterministic
// highest-preference solution, or a conflict with explanation.
func (s *Solver) Solve() Result {
	s.log = nil
	s.lastConflict = nil
	s.lastConflictDepth = -1
	s.lastConflictPkg = ""
	asg := &assignment{chosen: map[string]parsedVersion{}, reqs: map[string][]Requirement{}}
	// Deterministic root order.
	rootNames := make([]string, 0, len(s.roots))
	for name := range s.roots {
		rootNames = append(rootNames, name)
	}
	sort.Strings(rootNames)
	for _, name := range rootNames {
		asg.reqs[name] = append(asg.reqs[name], Requirement{Requirer: "", Constraint: s.roots[name].String()})
	}
	final := s.search(asg, 0)
	if final == nil {
		asgForConflict := s.lastConflict
		if asgForConflict == nil {
			asgForConflict = asg
		}
		return Result{OK: false, Conflict: s.buildConflict(asgForConflict), Log: s.log}
	}
	sol := map[string]string{}
	for name, pv := range final.chosen {
		sol[name] = pv.ver.String()
	}
	s.emit(0, "solution", "", "", "all requirements satisfied")
	return Result{OK: true, Solution: sol, Log: s.log}
}

// search picks the alphabetically smallest unresolved package and tries
// its candidates in descending version order. The fixed package order plus
// descending values makes the first solution found the deterministic,
// lexicographically maximal one — the same optimum BruteForceSolve finds.
func (s *Solver) search(asg *assignment, depth int) *assignment {
	name, cands, ok := s.nextPackage(asg)
	if !ok {
		return asg // complete
	}
	if len(cands) == 0 {
		s.emit(depth, "conflict", name, "", s.conflictReason(asg, name))
		if s.lastConflict == nil || depth >= s.lastConflictDepth {
			s.lastConflict = asg.clone()
			s.lastConflictDepth = depth
			s.lastConflictPkg = name
		}
		return nil
	}
	s.emit(depth, "select", name, "", fmt.Sprintf("%d candidate version(s)", len(cands)))
	for _, pv := range cands {
		next := asg.clone()
		next.chosen[name] = pv
		// Check the pick against every constraint already imposed on name.
		if !s.satisfiesAll(next, name, pv.ver) {
			s.emit(depth, "reject", name, pv.ver.String(), "violates an accumulated constraint")
			continue
		}
		s.emit(depth, "try", name, pv.ver.String(), "")
		// Impose this version's dependency constraints.
		depNames := make([]string, 0, len(pv.deps))
		for dep := range pv.deps {
			depNames = append(depNames, dep)
		}
		sort.Strings(depNames)
		for _, dep := range depNames {
			if _, chosen := next.chosen[dep]; chosen {
				// Dependency on an already-chosen package: just add the
				// requirement; satisfiesAll above will catch violations on
				// the next pass. A back-reference also closes a cycle.
				s.emit(depth+1, "cycle", dep, "", fmt.Sprintf("%s@%s closes a dependency cycle", name, pv.ver))
			}
			next.reqs[dep] = append(next.reqs[dep], Requirement{
				Requirer:   fmt.Sprintf("%s@%s", name, pv.ver),
				Constraint: pv.deps[dep].String(),
			})
		}
		// Verify already-chosen packages still satisfy newly added reqs.
		consistent := true
		for _, dep := range depNames {
			if pv2, chosen := next.chosen[dep]; chosen && !s.satisfiesAll(next, dep, pv2.ver) {
				s.emit(depth+1, "reject", dep, pv2.ver.String(),
					fmt.Sprintf("already chosen, but %s@%s requires %s", name, pv.ver, pv.deps[dep]))
				consistent = false
				break
			}
		}
		if !consistent {
			continue
		}
		if res := s.search(next, depth+1); res != nil {
			return res
		}
		s.emit(depth, "backtrack", name, pv.ver.String(), "no solution in this branch")
	}
	return nil
}

// nextPackage returns the alphabetically smallest unchosen package that
// has requirements, together with its admissible candidates;
// ok=false means everything is resolved.
func (s *Solver) nextPackage(asg *assignment) (string, []parsedVersion, bool) {
	names := make([]string, 0, len(asg.reqs))
	for name := range asg.reqs {
		if _, chosen := asg.chosen[name]; !chosen {
			names = append(names, name)
		}
	}
	if len(names) == 0 {
		return "", nil, false
	}
	sort.Strings(names)
	name := names[0]
	return name, s.candidates(asg, name), true
}

// candidates lists versions of name satisfying all requirements on it,
// highest first.
func (s *Solver) candidates(asg *assignment, name string) []parsedVersion {
	var out []parsedVersion
	for _, pv := range s.parsed[name] {
		if s.satisfiesAll(asg, name, pv.ver) {
			out = append(out, pv)
		}
	}
	return out
}

func (s *Solver) satisfiesAll(asg *assignment, name string, v semver.Version) bool {
	for _, req := range asg.reqs[name] {
		c, err := semver.ParseConstraint(req.Constraint)
		if err != nil { // validated at construction; be defensive
			return false
		}
		if !c.Allows(v) {
			return false
		}
	}
	return true
}

func (s *Solver) conflictReason(asg *assignment, name string) string {
	if len(s.parsed[name]) == 0 {
		return fmt.Sprintf("package %q has no versions in the registry", name)
	}
	return fmt.Sprintf("no version of %q satisfies all constraints: %s", name, describeReqs(asg.reqs[name]))
}

func describeReqs(reqs []Requirement) string {
	parts := make([]string, 0, len(reqs))
	for _, r := range reqs {
		if r.Requirer == "" {
			parts = append(parts, fmt.Sprintf("root requires %s", r.Constraint))
		} else {
			parts = append(parts, fmt.Sprintf("%s requires %s", r.Requirer, r.Constraint))
		}
	}
	sort.Strings(parts)
	out := ""
	for i, p := range parts {
		if i > 0 {
			out += "; "
		}
		out += p
	}
	return out
}

// buildConflict reconstructs the failing package and dependency chains
// from the assignment state captured at the deepest conflict.
func (s *Solver) buildConflict(asg *assignment) *Conflict {
	pkg := s.lastConflictPkg
	cf := &Conflict{Package: pkg, Reason: "no version combination satisfies all constraints"}
	if pkg != "" {
		cf.Reason = s.conflictReason(asg, pkg)
		cf.Chains = s.chainsTo(asg, pkg)
	}
	return cf
}

// chainsTo renders every requirement path from a root to name, walking
// upward from each requirement imposed on name through chosen packages.
func (s *Solver) chainsTo(asg *assignment, name string) []string {
	var chains []string
	for _, req := range asg.reqs[name] {
		chains = append(chains, s.renderChain(asg, name, req))
	}
	sort.Strings(chains)
	return chains
}

func (s *Solver) renderChain(asg *assignment, name string, req Requirement) string {
	if req.Requirer == "" {
		return fmt.Sprintf("root => %s %s", name, req.Constraint)
	}
	path := []string{fmt.Sprintf("%s => %s %s", req.Requirer, name, req.Constraint)}
	cur := req.Requirer // "pkg@ver"
	seen := map[string]bool{}
	for steps := 0; steps <= len(asg.chosen); steps++ {
		pkg, _ := splitRequirer(cur)
		if pkg == "" {
			break
		}
		if seen[pkg] {
			path = append(path, "(dependency cycle)")
			break
		}
		seen[pkg] = true
		up := pickParent(asg, pkg)
		if up == nil {
			break
		}
		if up.Requirer == "" {
			path = append([]string{fmt.Sprintf("root => %s %s", pkg, up.Constraint)}, path...)
			break
		}
		path = append([]string{fmt.Sprintf("%s => %s %s", up.Requirer, pkg, up.Constraint)}, path...)
		cur = up.Requirer
	}
	out := ""
	for i, seg := range path {
		if i > 0 {
			out += " -> "
		}
		out += seg
	}
	return out
}

// pickParent deterministically selects one requirement imposed on pkg:
// a root requirement if present, otherwise the lexicographically smallest
// requirer.
func pickParent(asg *assignment, pkg string) *Requirement {
	reqs := asg.reqs[pkg]
	if len(reqs) == 0 {
		return nil
	}
	for i := range reqs {
		if reqs[i].Requirer == "" {
			return &reqs[i]
		}
	}
	idx := 0
	for i := range reqs {
		if reqs[i].Requirer < reqs[idx].Requirer {
			idx = i
		}
	}
	return &reqs[idx]
}

func splitRequirer(r string) (pkg, ver string) {
	for i := len(r) - 1; i >= 0; i-- {
		if r[i] == '@' {
			return r[:i], r[i+1:]
		}
	}
	return r, ""
}
