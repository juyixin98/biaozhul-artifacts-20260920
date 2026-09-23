package solver

import (
	"fmt"
	"math/rand"
	"reflect"
	"testing"
)

// crossCheck asserts the backtracking solver and the exhaustive reference
// agree on solvability and on the exact solution.
func crossCheck(t *testing.T, reg Registry, roots map[string]string) {
	t.Helper()
	s, err := NewSolver(reg, roots)
	if err != nil {
		t.Fatalf("NewSolver: %v", err)
	}
	got := s.Solve()
	want, ok := BruteForceSolve(reg, roots)
	if got.OK != ok {
		t.Fatalf("solvability mismatch: solver ok=%v (%+v), brute force ok=%v (%v)",
			got.OK, got.Conflict, ok, want)
	}
	if !ok {
		return
	}
	if !reflect.DeepEqual(got.Solution, want) {
		t.Fatalf("solution mismatch:\nsolver:      %v\nbrute force: %v", got.Solution, want)
	}
}

func TestFixturesMatchBruteForce(t *testing.T) {
	fixtures := []struct {
		name  string
		reg   Registry
		roots map[string]string
	}{
		{
			name: "diamond resolvable",
			reg: Registry{
				"a": {vi("1.0.0", "b", "^1.4", "c", "^1.2")},
				"b": {vi("1.5.0", "d", "^2.0.0"), vi("1.4.0", "d", "^1.0.0")},
				"c": {vi("1.2.0", "d", "^1.0.0")},
				"d": {vi("2.0.0"), vi("1.2.0"), vi("1.0.0")},
			},
			roots: map[string]string{"a": "^1.0.0"},
		},
		{
			name: "diamond unsolvable",
			reg: Registry{
				"a": {vi("1.0.0", "b", "^1.5", "c", "^1.2")},
				"b": {vi("1.5.0", "d", "^2.0.0")},
				"c": {vi("1.2.0", "d", "^1.0.0")},
				"d": {vi("2.0.0"), vi("1.0.0")},
			},
			roots: map[string]string{"a": "^1.0.0"},
		},
		{
			name: "cycle resolvable",
			reg: Registry{
				"a": {vi("1.0.0", "b", "^1.0.0")},
				"b": {vi("1.0.0", "a", "^1.0.0")},
			},
			roots: map[string]string{"a": "^1.0.0"},
		},
		{
			name: "cycle unsolvable",
			reg: Registry{
				"a": {vi("1.0.0", "b", "^2.0.0")},
				"b": {vi("1.0.0", "a", "^1.0.0")},
			},
			roots: map[string]string{"a": "^1.0.0"},
		},
		{
			name: "prerelease gating",
			reg: Registry{
				"lib": {vi("1.0.0-rc.1"), vi("0.9.0")},
			},
			roots: map[string]string{"lib": "^0.9.0"},
		},
		{
			name: "prerelease admitted",
			reg: Registry{
				"lib": {vi("1.0.0-rc.1"), vi("0.9.0")},
			},
			roots: map[string]string{"lib": ">=1.0.0-rc.1 <2.0.0"},
		},
		{
			name: "no version satisfies",
			reg: Registry{
				"x": {vi("1.0.0"), vi("2.0.0")},
			},
			roots: map[string]string{"x": "^3.0.0"},
		},
	}
	for _, f := range fixtures {
		t.Run(f.name, func(t *testing.T) {
			crossCheck(t, f.reg, f.roots)
		})
	}
}

// TestRandomSmallGraphs generates random small registries (including
// cycles and prereleases) and cross-checks every one against the
// exhaustive reference.
func TestRandomSmallGraphs(t *testing.T) {
	rng := rand.New(rand.NewSource(20260922))
	versionPool := []string{
		"0.9.0", "1.0.0", "1.1.0", "1.2.0", "2.0.0", "1.0.0-rc.1",
	}
	constraintPool := []string{
		"*", "^1.0.0", "^0.9.0", "~1.0.0", ">=1.0.0 <2.0.0",
		"1.0.0", "<2.0.0", ">=1.1.0", "^2.0.0", ">=1.0.0-rc.1 <2.0.0",
	}
	for trial := 0; trial < 400; trial++ {
		n := 2 + rng.Intn(4) // 2..5 packages
		names := make([]string, n)
		for i := range names {
			names[i] = fmt.Sprintf("p%d", i)
		}
		reg := Registry{}
		for _, name := range names {
			nv := 1 + rng.Intn(3) // 1..3 versions
			perm := rng.Perm(len(versionPool))
			var versions []VersionInfo
			for i := 0; i < nv; i++ {
				v := VersionInfo{Version: versionPool[perm[i]]}
				// Random deps on up to 2 other packages (may form cycles).
				for d := 0; d < rng.Intn(3); d++ {
					dep := names[rng.Intn(n)]
					if dep == name {
						continue
					}
					if v.Deps == nil {
						v.Deps = map[string]string{}
					}
					v.Deps[dep] = constraintPool[rng.Intn(len(constraintPool))]
				}
				versions = append(versions, v)
			}
			reg[name] = versions
		}
		roots := map[string]string{
			"p0": constraintPool[rng.Intn(len(constraintPool))],
		}
		t.Run(fmt.Sprintf("trial-%03d", trial), func(t *testing.T) {
			crossCheck(t, reg, roots)
		})
	}
}
