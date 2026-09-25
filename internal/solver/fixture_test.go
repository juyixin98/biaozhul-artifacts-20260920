package solver_test

// 夹具驱动测试：testdata/fixtures/*.json 既是自动化输入，也是 README 里引用的请求样例。
// 每个夹具的 "_expect" 块只用于断言，送求解器前会被剥离。

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"depsolver/internal/registry"
	"depsolver/internal/service"
	"depsolver/internal/solver"
)

type fixtureExpect struct {
	Case             string            `json:"case"`
	Satisfiable      bool              `json:"satisfiable"`
	Selections       map[string]string `json:"selections"`
	ConflictType     string            `json:"conflictType"`
	ConflictPackage  string            `json:"conflictPackage"`
	ChainReachesRoot bool              `json:"chainReachesRoot"`
	CyclePackages    []string          `json:"cyclePackages"`
	MinBacktracks    int               `json:"minBacktracks"`
}

type fixtureFile struct {
	Expect fixtureExpect `json:"_expect"`
}

func loadFixtures(t *testing.T) []string {
	t.Helper()
	paths, err := filepath.Glob(filepath.Join("..", "..", "testdata", "fixtures", "*.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(paths) == 0 {
		t.Fatal("no fixtures found")
	}
	sort.Strings(paths)
	return paths
}

func runFixture(t *testing.T, path string) (fixtureExpect, *solver.Result) {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var head fixtureFile
	if err := json.Unmarshal(raw, &head); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
	// 剥离 _expect 后按正式请求解析，保证夹具不会依赖服务层放行未知字段。
	var generic map[string]json.RawMessage
	if err := json.Unmarshal(raw, &generic); err != nil {
		t.Fatal(err)
	}
	delete(generic, "_expect")
	body, err := json.Marshal(generic)
	if err != nil {
		t.Fatal(err)
	}
	var req service.ResolveRequest
	if err := json.Unmarshal(body, &req); err != nil {
		t.Fatalf("%s: request parse: %v", path, err)
	}
	reg, err := registry.Build(req.Registry)
	if err != nil {
		t.Fatalf("%s: registry build: %v", path, err)
	}
	res, err := solver.Solve(reg, req.Roots, solver.Options{IncludePrereleases: req.IncludePrereleases})
	if err != nil {
		t.Fatalf("%s: solve: %v", path, err)
	}
	return head.Expect, res
}

func TestFixtures(t *testing.T) {
	for _, path := range loadFixtures(t) {
		path := path
		name := strings.TrimSuffix(filepath.Base(path), ".json")
		t.Run(name, func(t *testing.T) {
			exp, res := runFixture(t, path)
			if res.Satisfiable != exp.Satisfiable {
				t.Fatalf("satisfiable=%v want %v; conflict=%+v",
					res.Satisfiable, exp.Satisfiable, res.Conflict)
			}
			if exp.Satisfiable {
				got := map[string]string{}
				for _, s := range res.Selections {
					got[s.Package] = s.Version
				}
				for pkg, wantV := range exp.Selections {
					if got[pkg] != wantV {
						t.Errorf("selection %s=%q want %q (all=%v)", pkg, got[pkg], wantV, got)
					}
				}
				if len(exp.Selections) != len(got) {
					t.Errorf("extra selections: got %v want %v", got, exp.Selections)
				}
			} else {
				if res.Conflict == nil {
					t.Fatal("unsat but no conflict body")
				}
				if exp.ConflictType != "" && res.Conflict.Type != exp.ConflictType {
					t.Errorf("conflict type=%q want %q", res.Conflict.Type, exp.ConflictType)
				}
				if exp.ConflictPackage != "" && res.Conflict.Package != exp.ConflictPackage {
					t.Errorf("conflict package=%q want %q", res.Conflict.Package, exp.ConflictPackage)
				}
				if exp.ChainReachesRoot {
					found := false
					for _, h := range res.Conflict.Chain {
						if h.RequiredBy == "root" {
							found = true
						}
					}
					if !found {
						t.Errorf("explanatory chain does not reach root: %+v", res.Conflict.Chain)
					}
				}
			}
			if exp.MinBacktracks > 0 && res.Stats.Backtracks < exp.MinBacktracks {
				t.Errorf("backtracks=%d want >=%d", res.Stats.Backtracks, exp.MinBacktracks)
			}
			if len(exp.CyclePackages) > 0 {
				want := map[string]bool{}
				for _, p := range exp.CyclePackages {
					want[p] = true
				}
				found := false
				for _, cyc := range res.Cycles {
					got := map[string]bool{}
					for _, p := range cyc.Packages {
						got[p] = true
					}
					if len(got) == len(want) {
						all := true
						for p := range want {
							if !got[p] {
								all = false
							}
						}
						if all {
							found = true
						}
					}
				}
				if !found {
					t.Errorf("expected cycle over %v, got %+v", want, res.Cycles)
				}
			}
		})
	}
}

// TestFixturesHaveTrace 保证每个夹具都产出非空决策轨迹（回溯决策可解释）。
func TestFixturesHaveTrace(t *testing.T) {
	for _, path := range loadFixtures(t) {
		exp, res := runFixture(t, path)
		if len(res.Trace) == 0 {
			t.Errorf("%s: empty trace", exp.Case)
		}
	}
}
