package solver

import (
	"testing"

	"depsolver/internal/model"
	"depsolver/internal/registry"
)

// regBuilder 是测试用的注册表构造器。
type regBuilder struct {
	pkgs map[string][]model.PackageVersion
}

func newRegistry() *regBuilder {
	return &regBuilder{pkgs: map[string][]model.PackageVersion{}}
}

func (b *regBuilder) add(name string, version string, deps ...model.Dependency) *regBuilder {
	b.pkgs[name] = append(b.pkgs[name], model.PackageVersion{Version: version, Deps: deps})
	return b
}

func (b *regBuilder) build(t *testing.T) *registry.Registry {
	t.Helper()
	in := model.RegistryInput{}
	for name, vs := range b.pkgs {
		in.Packages = append(in.Packages, model.Package{Name: name, Versions: vs})
	}
	reg, err := registry.Build(in)
	if err != nil {
		t.Fatalf("registry build: %v", err)
	}
	return reg
}

func dep(name, con string) model.Dependency {
	return model.Dependency{Name: name, Constraint: con}
}

func selectedMap(res *Result) map[string]string {
	m := map[string]string{}
	for _, s := range res.Selections {
		m[s.Package] = s.Version
	}
	return m
}

// TestDiamondCompatible：经典菱形，两端约束相容，选中 B 的最新兼容版本。
func TestDiamondCompatible(t *testing.T) {
	reg := newRegistry().
		add("root", "1.0.0", dep("left", "^1.0.0"), dep("right", "^1.2.0")).
		add("left", "1.0.0", dep("common", ">=1.0.0 <2.0.0")).
		add("left", "1.1.0", dep("common", ">=1.1.0 <2.0.0")).
		add("right", "1.2.0", dep("common", "~1.2.0")).
		add("common", "1.0.0").
		add("common", "1.1.0").
		add("common", "1.2.0").
		add("common", "1.2.1").
		add("common", "2.0.0").
		build(t)

	res, err := Solve(reg, []model.Dependency{dep("root", "1.0.0")}, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Satisfiable {
		t.Fatalf("expected SAT: %+v", res.Conflict)
	}
	sel := selectedMap(res)
	if sel["root"] != "1.0.0" || sel["left"] != "1.1.0" || sel["right"] != "1.2.0" {
		t.Fatalf("unexpected selections: %v", sel)
	}
	if sel["common"] != "1.2.1" {
		t.Fatalf("common should resolve to 1.2.1, got %q", sel["common"])
	}
}

// TestDiamondConflictBacktracks：菱形冲突——最新的 left 与 right 对 common
// 的约束不相容，但回退到 left 旧版本后可解。验证回溯确实发生并选中旧版本。
func TestDiamondConflictBacktracks(t *testing.T) {
	reg := newRegistry().
		add("root", "1.0.0", dep("left", "^1.0.0"), dep("right", "^1.0.0")).
		add("left", "1.2.0", dep("common", "^2.0.0")).
		add("left", "1.1.0", dep("common", "^1.0.0")).
		add("right", "1.0.0", dep("common", "~1.3.0")).
		add("common", "1.3.0").
		add("common", "1.3.5").
		add("common", "2.0.0").
		build(t)

	res, err := Solve(reg, []model.Dependency{dep("root", "1.0.0")}, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Satisfiable {
		t.Fatalf("expected SAT after backtrack: %+v", res.Conflict)
	}
	sel := selectedMap(res)
	if sel["left"] != "1.1.0" {
		t.Fatalf("left should backtrack to 1.1.0, got %q", sel["left"])
	}
	if sel["common"] != "1.3.5" {
		t.Fatalf("common should be 1.3.5, got %q", sel["common"])
	}
	if res.Stats.Backtracks == 0 {
		t.Fatal("expected at least one backtrack in stats")
	}
}

// TestUnsolvableDiamond：任何 left 版本都与 right 不相容 -> UNSAT，
// 且冲突体携带依赖链。
func TestUnsolvableDiamond(t *testing.T) {
	reg := newRegistry().
		add("root", "1.0.0", dep("left", "^1.0.0"), dep("right", "^1.0.0")).
		add("left", "1.2.0", dep("common", "^2.0.0")).
		add("left", "1.1.0", dep("common", ">=2.0.0")).
		add("right", "1.0.0", dep("common", "~1.3.0")).
		add("common", "1.3.0").
		add("common", "2.0.0").
		build(t)

	res, err := Solve(reg, []model.Dependency{dep("root", "1.0.0")}, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if res.Satisfiable {
		t.Fatal("expected UNSAT")
	}
	if res.Conflict == nil || res.Conflict.Package != "common" {
		t.Fatalf("expected conflict on common, got %+v", res.Conflict)
	}
	if len(res.Conflict.Chain) == 0 {
		t.Fatal("conflict should carry an explanatory chain")
	}
	foundRoot := false
	for _, h := range res.Conflict.Chain {
		if h.RequiredBy == "root" {
			foundRoot = true
		}
	}
	if !foundRoot {
		t.Fatalf("chain should reach the root: %+v", res.Conflict.Chain)
	}
}

// TestPrereleaseGate：默认门控下不会选预发布；显式提及时可以；
// includePrereleases 打开后全部可选。
func TestPrereleaseGate(t *testing.T) {
	reg := newRegistry().
		add("app", "1.0.0", dep("lib", "^1.0.0")).
		add("lib", "1.0.0").
		add("lib", "1.1.0-beta.1").
		build(t)

	// 默认：稳定版
	res, _ := Solve(reg, []model.Dependency{dep("app", "1.0.0")}, Options{})
	if got := selectedMap(res)["lib"]; got != "1.0.0" {
		t.Fatalf("gate off: expected 1.0.0, got %q", got)
	}

	// 显式在约束里提及该预发布基元 -> 允许
	reg2 := newRegistry().
		add("app", "1.0.0", dep("lib", ">=1.1.0-beta.1")).
		add("lib", "1.0.0").
		add("lib", "1.1.0-beta.1").
		build(t)
	res, _ = Solve(reg2, []model.Dependency{dep("app", "1.0.0")}, Options{})
	if !res.Satisfiable {
		t.Fatalf("explicit prerelease should be SAT: %+v", res.Conflict)
	}
	if got := selectedMap(res)["lib"]; got != "1.1.0-beta.1" {
		t.Fatalf("explicit mention: expected 1.1.0-beta.1, got %q", got)
	}

	// 只有预发布版本、且无人提及时 -> UNSAT（门控在交集预检阶段排除 beta）
	preOnly := newRegistry().
		add("lib", "1.1.0-beta.1").
		build(t)
	res, _ = Solve(preOnly, []model.Dependency{
		{Name: "lib", Constraint: "^1.0.0"},
	}, Options{})
	if res.Satisfiable {
		t.Fatal("prerelease-only lib under ^1.0.0 should be UNSAT with gate")
	}
	if res.Conflict.Type != "constraint-clash" {
		t.Fatalf("expected constraint-clash, got %s", res.Conflict.Type)
	}
	if len(res.Conflict.Rejected) == 0 {
		t.Fatal("conflict should explain why the prerelease version was rejected")
	}
	var sawGate bool
	for _, rj := range res.Conflict.Rejected {
		if rj.Version == "1.1.0-beta.1" {
			sawGate = true
		}
	}
	if !sawGate {
		t.Fatalf("rejected list should mention beta: %+v", res.Conflict.Rejected)
	}

	// 打开 includePrereleases -> 选到 beta
	res, _ = Solve(reg, []model.Dependency{dep("app", "1.0.0")}, Options{IncludePrereleases: true})
	if got := selectedMap(res)["lib"]; got != "1.1.0-beta.1" {
		t.Fatalf("includePrereleases: expected beta, got %q", got)
	}
}

// TestCycleDetected：A<->B 循环在 flat 模型下可解，但必须在 Cycles 中报告。
func TestCycleDetected(t *testing.T) {
	reg := newRegistry().
		add("a", "1.0.0", dep("b", "^1.0.0")).
		add("b", "1.0.0", dep("a", "^1.0.0")).
		build(t)
	res, err := Solve(reg, []model.Dependency{dep("a", "^1.0.0")}, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Satisfiable {
		t.Fatalf("cycle should still be SAT: %+v", res.Conflict)
	}
	if len(res.Cycles) != 1 {
		t.Fatalf("expected exactly 1 cycle, got %+v", res.Cycles)
	}
	got := map[string]bool{}
	for _, p := range res.Cycles[0].Packages {
		got[p] = true
	}
	if !got["a"] || !got["b"] {
		t.Fatalf("cycle should contain a and b: %v", res.Cycles[0].Packages)
	}
}

// TestSelfDependency：自环也是循环。
func TestSelfDependency(t *testing.T) {
	reg := newRegistry().
		add("a", "1.0.0", dep("a", "^1.0.0")).
		build(t)
	res, err := Solve(reg, []model.Dependency{dep("a", "^1.0.0")}, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Satisfiable || len(res.Cycles) != 1 || res.Cycles[0].Packages[0] != "a" {
		t.Fatalf("expected self-cycle, got sat=%v cycles=%+v", res.Satisfiable, res.Cycles)
	}
}

// TestUnknownPackage：依赖指向不存在的包 -> unknown-package 冲突带链。
func TestUnknownPackage(t *testing.T) {
	reg := newRegistry().
		add("app", "1.0.0", dep("ghost", "^1.0.0")).
		build(t)
	res, err := Solve(reg, []model.Dependency{dep("app", "1.0.0")}, Options{})
	if err != nil {
		t.Fatal(err)
	}
	if res.Satisfiable || res.Conflict.Type != "unknown-package" || res.Conflict.Package != "ghost" {
		t.Fatalf("expected unknown-package on ghost, got %+v", res.Conflict)
	}
}

// TestDeterministic：同一输入连解两次，选择与轨迹长度一致。
func TestDeterministic(t *testing.T) {
	reg := newRegistry().
		add("root", "1.0.0", dep("left", "^1"), dep("right", "^1")).
		add("left", "1.3.0", dep("common", "^1.2")).
		add("left", "1.2.0", dep("common", "~1.2.0")).
		add("right", "1.1.0", dep("common", ">=1.2.5")).
		add("common", "1.2.0").
		add("common", "1.2.5").
		add("common", "1.2.9").
		build(t)
	roots := []model.Dependency{dep("root", "1.0.0")}
	r1, _ := Solve(reg, roots, Options{})
	r2, _ := Solve(reg, roots, Options{})
	if !r1.Satisfiable || !r2.Satisfiable {
		t.Fatal("both should be SAT")
	}
	if len(r1.Selections) != len(r2.Selections) {
		t.Fatal("selection length differs")
	}
	for i := range r1.Selections {
		if r1.Selections[i] != r2.Selections[i] {
			t.Fatalf("nondeterministic: %v vs %v", r1.Selections, r2.Selections)
		}
	}
	if len(r1.Trace) != len(r2.Trace) {
		t.Fatal("trace length differs between runs")
	}
}

// TestTraceRecordsBacktrack：回溯在轨迹中同时留下 try / conflict / backtrack 事件。
func TestTraceRecordsBacktrack(t *testing.T) {
	reg := newRegistry().
		add("root", "1.0.0", dep("a", "^1"), dep("b", "^1")).
		add("a", "1.1.0", dep("c", "^2")).
		add("a", "1.0.0", dep("c", "^1")).
		add("b", "1.0.0", dep("c", "~1.0.0")).
		add("c", "1.0.0").
		add("c", "2.0.0").
		build(t)
	res, _ := Solve(reg, []model.Dependency{dep("root", "1.0.0")}, Options{})
	if !res.Satisfiable {
		t.Fatalf("expected SAT: %+v", res.Conflict)
	}
	kinds := map[string]int{}
	for _, e := range res.Trace {
		kinds[e.Kind]++
	}
	for _, k := range []string{"try", "activate", "backtrack", "add-requirement"} {
		if kinds[k] == 0 {
			t.Errorf("trace missing %q events (have %v)", k, kinds)
		}
	}
	if res.Stats.Backtracks != kinds["backtrack"] {
		t.Errorf("stats backtracks %d != trace backtrack events %d", res.Stats.Backtracks, kinds["backtrack"])
	}
}
