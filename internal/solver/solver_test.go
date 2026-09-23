package solver

import (
	"encoding/json"
	"testing"
)

func vi(version string, deps ...string) VersionInfo {
	d := map[string]string{}
	for i := 0; i+1 < len(deps); i += 2 {
		d[deps[i]] = deps[i+1]
	}
	return VersionInfo{Version: version, Deps: d}
}

func mustSolve(t *testing.T, reg Registry, roots map[string]string) Result {
	t.Helper()
	s, err := NewSolver(reg, roots)
	if err != nil {
		t.Fatalf("NewSolver: %v", err)
	}
	return s.Solve()
}

func assertSolution(t *testing.T, r Result, want map[string]string) {
	t.Helper()
	if !r.OK {
		t.Fatalf("expected solution, got conflict: %+v\nlog: %v", r.Conflict, r.Log)
	}
	if len(r.Solution) != len(want) {
		t.Fatalf("solution = %v, want %v", r.Solution, want)
	}
	for k, v := range want {
		if r.Solution[k] != v {
			t.Errorf("solution[%s] = %q, want %q (full: %v)", k, r.Solution[k], v, r.Solution)
		}
	}
}

// Diamond resolvable: a -> {b, c} -> d. b 1.5 prefers d 2.x but c forces
// d 1.x, so the solver must backtrack b from 1.5.0 to 1.4.0.
func TestDiamondBacktracks(t *testing.T) {
	reg := Registry{
		"a": {vi("1.0.0", "b", "^1.4", "c", "^1.2")},
		"b": {
			vi("1.5.0", "d", "^2.0.0"),
			vi("1.4.0", "d", "^1.0.0"),
		},
		"c": {vi("1.2.0", "d", "^1.0.0")},
		"d": {vi("2.0.0"), vi("1.2.0"), vi("1.0.0")},
	}
	r := mustSolve(t, reg, map[string]string{"a": "^1.0.0"})
	assertSolution(t, r, map[string]string{
		"a": "1.0.0", "b": "1.4.0", "c": "1.2.0", "d": "1.2.0",
	})
	kinds := map[string]int{}
	for _, d := range r.Log {
		kinds[d.Kind]++
	}
	if kinds["backtrack"] == 0 {
		t.Errorf("expected a backtrack entry in log, got kinds=%v", kinds)
	}
	if kinds["try"] < 2 {
		t.Errorf("expected at least two try entries, got kinds=%v", kinds)
	}
}

// Diamond with no solution: b exists only at 1.5.0 (needs d ^2), c only at
// 1.2.0 (needs d ^1).
func TestDiamondUnsolvable(t *testing.T) {
	reg := Registry{
		"a": {vi("1.0.0", "b", "^1.5", "c", "^1.2")},
		"b": {vi("1.5.0", "d", "^2.0.0")},
		"c": {vi("1.2.0", "d", "^1.0.0")},
		"d": {vi("2.0.0"), vi("1.0.0")},
	}
	r := mustSolve(t, reg, map[string]string{"a": "^1.0.0"})
	if r.OK {
		t.Fatalf("expected conflict, got solution %v", r.Solution)
	}
	if r.Conflict == nil || r.Conflict.Package != "d" {
		t.Fatalf("expected conflict on d, got %+v", r.Conflict)
	}
	joined := ""
	for _, c := range r.Conflict.Chains {
		joined += c + "\n"
		if !contains(c, "root") || !contains(c, "=> d") {
			t.Errorf("chain must start at root and end at d: %q", c)
		}
	}
	if !contains(joined, "b@1.5.0") || !contains(joined, "c@1.2.0") {
		t.Errorf("chains must name both conflicting requirers:\n%s", joined)
	}
}

// Root-level direct conflict.
func TestRootConflict(t *testing.T) {
	reg := Registry{
		"x": {vi("2.0.0"), vi("1.5.0"), vi("1.0.0")},
	}
	r := mustSolve(t, reg, map[string]string{"x": ">=2.0.0 <3.0.0 <2.0.0"})
	if r.OK {
		t.Fatal("expected conflict")
	}
	if r.Conflict.Package != "x" || !contains(r.Conflict.Reason, "root requires") {
		t.Fatalf("unexpected conflict: %+v", r.Conflict)
	}
}

func TestPrereleaseSelection(t *testing.T) {
	reg := Registry{
		"lib": {vi("1.0.0-rc.1"), vi("0.9.0")},
	}
	// A 0.x range must not jump to the 1.0.0 prerelease.
	r := mustSolve(t, reg, map[string]string{"lib": "^0.9.0"})
	assertSolution(t, r, map[string]string{"lib": "0.9.0"})

	// An explicit prerelease comparator admits it.
	r = mustSolve(t, reg, map[string]string{"lib": ">=1.0.0-rc.1 <2.0.0"})
	assertSolution(t, r, map[string]string{"lib": "1.0.0-rc.1"})

	// A range without a prerelease cannot be satisfied by only prereleases.
	reg2 := Registry{"lib": {vi("1.0.0-rc.1"), vi("1.0.0-beta")}}
	r = mustSolve(t, reg2, map[string]string{"lib": "^1.0.0"})
	if r.OK {
		t.Fatalf("expected no solution from prereleases, got %v", r.Solution)
	}
	if !contains(r.Conflict.Reason, "no version") {
		t.Errorf("unexpected reason: %s", r.Conflict.Reason)
	}
}

func TestCycleResolves(t *testing.T) {
	reg := Registry{
		"a": {vi("1.0.0", "b", "^1.0.0")},
		"b": {vi("1.0.0", "a", "^1.0.0")},
	}
	r := mustSolve(t, reg, map[string]string{"a": "^1.0.0"})
	assertSolution(t, r, map[string]string{"a": "1.0.0", "b": "1.0.0"})
	sawCycle := false
	for _, d := range r.Log {
		if d.Kind == "cycle" && contains(d.Reason, "b@1.0.0") {
			sawCycle = true
		}
	}
	if !sawCycle {
		t.Errorf("expected a cycle log entry, got %v", r.Log)
	}
}

func TestCycleUnsolvable(t *testing.T) {
	reg := Registry{
		"a": {vi("1.0.0", "b", "^2.0.0")},
		"b": {vi("1.0.0", "a", "^1.0.0")},
	}
	r := mustSolve(t, reg, map[string]string{"a": "^1.0.0"})
	if r.OK {
		t.Fatalf("expected conflict, got %v", r.Solution)
	}
}

func TestUnknownPackageRejected(t *testing.T) {
	reg := Registry{"a": {vi("1.0.0", "b", "^1.0.0")}}
	if _, err := NewSolver(reg, map[string]string{"a": "^1.0.0"}); err == nil {
		t.Fatal("expected error for unknown dependency b")
	}
	if _, err := NewSolver(reg, map[string]string{"ghost": "^1.0.0"}); err == nil {
		t.Fatal("expected error for unknown root package")
	}
}

func TestDeterministic(t *testing.T) {
	reg := Registry{
		"a": {
			vi("1.1.0", "b", "^1.0.0", "c", "^1.0.0"),
			vi("1.0.0", "b", "^1.0.0"),
		},
		"b": {vi("1.5.0"), vi("1.4.0"), vi("1.0.0")},
		"c": {vi("1.3.0"), vi("1.0.0")},
	}
	first := mustSolve(t, reg, map[string]string{"a": "^1.0.0"})
	for i := 0; i < 5; i++ {
		r := mustSolve(t, reg, map[string]string{"a": "^1.0.0"})
		m1, _ := json.Marshal(first)
		m2, _ := json.Marshal(r)
		if string(m1) != string(m2) {
			t.Fatalf("non-deterministic result:\n%s\n%s", m1, m2)
		}
	}
}

// Highest versions preferred when everything is compatible.
func TestPicksHighest(t *testing.T) {
	reg := Registry{
		"a": {vi("1.0.0", "b", "^1.0.0"), vi("0.9.0")},
		"b": {vi("1.9.0"), vi("1.0.0")},
	}
	r := mustSolve(t, reg, map[string]string{"a": "^1.0.0"})
	assertSolution(t, r, map[string]string{"a": "1.0.0", "b": "1.9.0"})
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (s == sub || indexOf(s, sub) >= 0)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
