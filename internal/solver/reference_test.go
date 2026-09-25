package solver

import (
	"sort"
	"testing"

	"depsolver/internal/model"
	"depsolver/internal/registry"
	"depsolver/internal/semver"
)

// bruteForce 是独立于求解器的小图穷举参考：枚举“注册表中每个包取某个版本或缺席”
// 的完整组合（含缺席），找出全部可行完整指派。
//
// 完整指派可行当且仅当：
//  1. 每个根包都被选中且满足根约束；
//  2. 对每个被选中的包，其每条依赖的包也被选中、版本满足约束、通过同一套预发布门控；
//  3. 所有被选中的版本都是该包的真实版本。
//
// 函数返回可行解数量（fixture 图都很小，组合数可控）。
func bruteForce(t *testing.T, reg *registry.Registry, roots []model.Dependency, includePre bool) int {
	t.Helper()
	names := reg.Names()
	type slot struct {
		name    string
		raw     string
		version semver.Version
	}
	// domains[name] = 该包的可选版本槽（注册表已降序，这里排序保证独立于求解器）
	domains := map[string][]slot{}
	for _, n := range names {
		pkg := reg.Package(n)
		vs := append([]registry.ParsedVersion(nil), pkg.Versions...)
		sort.Slice(vs, func(i, j int) bool { return semver.Compare(vs[i].V, vs[j].V) < 0 })
		for _, pv := range vs {
			domains[n] = append(domains[n], slot{n, pv.Raw, pv.V})
		}
	}

	rootByName := map[string]model.Dependency{}
	for _, r := range roots {
		rootByName[r.Name] = r
	}

	assignment := map[string]slot{}
	feasible := 0

	var validate func() bool
	validate = func() bool {
		// 根必须在场且满足约束
		for _, r := range roots {
			s, ok := assignment[r.Name]
			if !ok {
				return false
			}
			c := mustCon(t, r.Constraint)
			if !c.Satisfy(s.version, includePre) {
				return false
			}
			if s.version.IsPrerelease() && !includePre && !c.MentionsPrerelease(s.version.Key()) {
				return false
			}
		}
		// 每个在场包的依赖必须闭包且满足
		for name, s := range assignment {
			pkg := reg.Package(name)
			var pv *registry.ParsedVersion
			for i := range pkg.Versions {
				if pkg.Versions[i].Raw == s.raw {
					pv = &pkg.Versions[i]
				}
			}
			if pv == nil {
				return false
			}
			for _, d := range pv.Deps {
				ds, ok := assignment[d.Name]
				if !ok {
					return false
				}
				if !d.Constraint.Satisfy(ds.version, includePre) {
					return false
				}
				if ds.version.IsPrerelease() && !includePre && !d.Constraint.MentionsPrerelease(ds.version.Key()) {
					return false
				}
			}
		}
		// 所有在场节点必须从某个根可达（闭包已由上面保证，这里等价于“没有多余的孤立包”）
		reach := map[string]bool{}
		queue := []string{}
		for _, r := range roots {
			if _, ok := assignment[r.Name]; ok && !reach[r.Name] {
				reach[r.Name] = true
				queue = append(queue, r.Name)
			}
		}
		for len(queue) > 0 {
			cur := queue[0]
			queue = queue[1:]
			pkg := reg.Package(cur)
			for _, pv := range pkg.Versions {
				if pv.Raw != assignment[cur].raw {
					continue
				}
				for _, d := range pv.Deps {
					if _, ok := assignment[d.Name]; ok && !reach[d.Name] {
						reach[d.Name] = true
						queue = append(queue, d.Name)
					}
				}
			}
		}
		for n := range assignment {
			if !reach[n] {
				return false
			}
		}
		return true
	}

	var enum func(idx int)
	enum = func(idx int) {
		if idx == len(names) {
			if validate() {
				feasible++
			}
			return
		}
		n := names[idx]
		// 缺席分支
		enum(idx + 1)
		// 每个版本一个分支
		for _, s := range domains[n] {
			assignment[n] = s
			enum(idx + 1)
			delete(assignment, n)
		}
	}
	enum(0)
	return feasible
}

func mustCon(t *testing.T, s string) semver.Constraint {
	t.Helper()
	c, err := semver.ParseConstraint(s)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

// greedyOptimal 用穷举验证求解器的贪心性质：沿最终成功路径上的每个 select，
// 固定“当时已经在成功解中被选定的包”，该包所选版本必须是“仍存在可行补全”的最新版本。
func greedyOptimal(t *testing.T, reg *registry.Registry, roots []model.Dependency, includePre bool, res *Result) {
	t.Helper()
	if !res.Satisfiable {
		return
	}
	// 按深度重放成功路径：深度 d 的 activate 在 dfs(d) 内发生。
	// 成功路径上的事件是那些没有被后续 backtrack 回滚的 activate。
	// 用“版本最终出现在 Selections 里”来确认该 activate 属于成功路径。
	final := map[string]string{}
	for _, s := range res.Selections {
		final[s.Package] = s.Version
	}
	type step struct {
		pkg, ver string
	}
	var path []step
	activeDepth := map[string]int{}
	for _, e := range res.Trace {
		switch e.Kind {
		case "activate":
			if final[e.Package] == e.Version {
				path = append(path, step{e.Package, e.Version})
				activeDepth[e.Package] = e.Depth
			}
		}
	}

	// 重放路径：每一步之前，已固定前缀；检查该包所有更新候选是否都不存在可行补全。
	fixed := map[string]string{}
	for _, st := range path {
		pkg := reg.Package(st.pkg)
		cur := mustVersion(t, st.ver)
		for _, pv := range pkg.Versions { // 降序
			if semver.Compare(pv.V, cur) <= 0 {
				break // 只检查比实际选择更新的候选
			}
			trial := cloneFixed(fixed)
			trial[st.pkg] = pv.Raw
			if completable(t, reg, roots, includePre, trial) {
				t.Fatalf("greedy violated: %q chose %s but newer %s admits a feasible completion (fixed=%v)",
					st.pkg, st.ver, pv.Raw, fixed)
			}
		}
		fixed[st.pkg] = st.ver
	}
}

func mustVersion(t *testing.T, s string) semver.Version {
	t.Helper()
	v, err := semver.Parse(s)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func cloneFixed(m map[string]string) map[string]string {
	out := make(map[string]string, len(m)+1)
	for k, v := range m {
		out[k] = v
	}
	return out
}

// completable 在固定指派 fixed 之上再次穷举：是否存在包含 fixed 的可行完整指派。
func completable(t *testing.T, reg *registry.Registry, roots []model.Dependency, includePre bool, fixed map[string]string) bool {
	names := reg.Names()
	pkgByName := map[string]*registry.PackageEntry{}
	for _, n := range names {
		pkgByName[n] = reg.Package(n)
	}
	assignment := map[string]registry.ParsedVersion{}
	for n, v := range fixed {
		var found *registry.ParsedVersion
		for _, pv := range pkgByName[n].Versions {
			if pv.Raw == v {
				pv := pv
				found = &pv
			}
		}
		if found == nil {
			return false
		}
		assignment[n] = *found
	}

	var go2 func(idx int) bool
	go2 = func(idx int) bool {
		if idx == len(names) {
			return validAssignment(t, reg, roots, includePre, assignment)
		}
		n := names[idx]
		if _, pre := assignment[n]; pre {
			return go2(idx + 1)
		}
		// 缺席
		if go2(idx + 1) {
			return true
		}
		for _, pv := range pkgByName[n].Versions {
			assignment[n] = pv
			if go2(idx + 1) {
				return true
			}
			delete(assignment, n)
		}
		return false
	}
	return go2(0)
}

func validAssignment(t *testing.T, reg *registry.Registry, roots []model.Dependency, includePre bool, a map[string]registry.ParsedVersion) bool {
	for _, r := range roots {
		pv, ok := a[r.Name]
		if !ok {
			return false
		}
		c := mustCon(t, r.Constraint)
		if !c.Satisfy(pv.V, includePre) {
			return false
		}
		if pv.V.IsPrerelease() && !includePre && !c.MentionsPrerelease(pv.V.Key()) {
			return false
		}
	}
	reach := map[string]bool{}
	queue := []string{}
	for _, r := range roots {
		if _, ok := a[r.Name]; ok && !reach[r.Name] {
			reach[r.Name] = true
			queue = append(queue, r.Name)
		}
	}
	for len(queue) > 0 {
		cur := queue[0]
		queue = queue[1:]
		pv := a[cur]
		for _, d := range pv.Deps {
			dv, ok := a[d.Name]
			if !ok {
				return false
			}
			if !d.Constraint.Satisfy(dv.V, includePre) {
				return false
			}
			if dv.V.IsPrerelease() && !includePre && !d.Constraint.MentionsPrerelease(dv.V.Key()) {
				return false
			}
			if !reach[d.Name] {
				reach[d.Name] = true
				queue = append(queue, d.Name)
			}
		}
	}
	for n := range a {
		if !reach[n] {
			return false
		}
	}
	return true
}

// 以下是“验收夹具”级别的穷举交叉校验：对每张小图同时运行求解器与独立穷举。

func TestExhaustiveDiamondCompatible(t *testing.T) {
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
	roots := []model.Dependency{dep("root", "1.0.0")}
	if n := bruteForce(t, reg, roots, false); n == 0 {
		t.Fatal("reference says UNSAT but fixture should be feasible")
	} else {
		t.Logf("diamond-compatible feasible full assignments: %d", n)
	}
	res, _ := Solve(reg, roots, Options{})
	if !res.Satisfiable {
		t.Fatalf("solver says UNSAT: %+v", res.Conflict)
	}
	greedyOptimal(t, reg, roots, false, res)
}

func TestExhaustiveBacktrackFixture(t *testing.T) {
	reg := newRegistry().
		add("root", "1.0.0", dep("left", "^1.0.0"), dep("right", "^1.0.0")).
		add("left", "1.2.0", dep("common", "^2.0.0")).
		add("left", "1.1.0", dep("common", "^1.0.0")).
		add("right", "1.0.0", dep("common", "~1.3.0")).
		add("common", "1.3.0").
		add("common", "1.3.5").
		add("common", "2.0.0").
		build(t)
	roots := []model.Dependency{dep("root", "1.0.0")}
	if n := bruteForce(t, reg, roots, false); n == 0 {
		t.Fatal("reference says UNSAT but backtrack fixture should be feasible")
	} else {
		t.Logf("backtrack fixture feasible full assignments: %d", n)
	}
	res, _ := Solve(reg, roots, Options{})
	if !res.Satisfiable {
		t.Fatal("solver UNSAT but reference SAT")
	}
	if v := selectedMap(res)["left"]; v != "1.1.0" {
		t.Fatalf("expected backtrack to left 1.1.0, got %s", v)
	}
	greedyOptimal(t, reg, roots, false, res)
}

func TestExhaustiveUnsolvable(t *testing.T) {
	reg := newRegistry().
		add("root", "1.0.0", dep("left", "^1.0.0"), dep("right", "^1.0.0")).
		add("left", "1.2.0", dep("common", "^2.0.0")).
		add("left", "1.1.0", dep("common", ">=2.0.0")).
		add("right", "1.0.0", dep("common", "~1.3.0")).
		add("common", "1.3.0").
		add("common", "2.0.0").
		build(t)
	roots := []model.Dependency{dep("root", "1.0.0")}
	if n := bruteForce(t, reg, roots, false); n != 0 {
		t.Fatalf("reference says %d feasible but fixture should be UNSAT", n)
	}
	res, _ := Solve(reg, roots, Options{})
	if res.Satisfiable {
		t.Fatal("solver SAT but reference UNSAT")
	}
}

func TestExhaustivePrerelease(t *testing.T) {
	reg := newRegistry().
		add("app", "1.0.0", dep("lib", "^1.0.0")).
		add("lib", "1.0.0").
		add("lib", "1.1.0-beta.1").
		add("lib", "1.1.0-beta.2").
		add("lib", "2.0.0-alpha.1").
		build(t)
	roots := []model.Dependency{dep("app", "1.0.0")}
	// 默认门控：穷举应只剩稳定版解
	if n := bruteForce(t, reg, roots, false); n == 0 {
		t.Fatal("gate-on reference unexpectedly UNSAT")
	}
	res, _ := Solve(reg, roots, Options{})
	if !res.Satisfiable || selectedMap(res)["lib"] != "1.0.0" {
		t.Fatalf("gate-on solve mismatch: %+v", res)
	}
	greedyOptimal(t, reg, roots, false, res)

	// 打开预发布
	res2, _ := Solve(reg, roots, Options{IncludePrereleases: true})
	if !res2.Satisfiable || selectedMap(res2)["lib"] != "2.0.0-alpha.1" {
		t.Fatalf("include-prereleases mismatch: %+v", res2)
	}
	greedyOptimal(t, reg, roots, true, res2)
}

func TestExhaustiveCycle(t *testing.T) {
	reg := newRegistry().
		add("a", "1.0.0", dep("b", "^1.0.0")).
		add("a", "1.1.0", dep("b", "^1.0.0")).
		add("b", "1.0.0", dep("a", "^1.0.0")).
		build(t)
	roots := []model.Dependency{dep("a", "^1.0.0")}
	n := bruteForce(t, reg, roots, false)
	if n == 0 {
		t.Fatal("cycle fixture should be feasible (flat resolution allows cycles)")
	}
	t.Logf("cycle fixture feasible full assignments: %d", n)
	res, _ := Solve(reg, roots, Options{})
	if !res.Satisfiable {
		t.Fatalf("solver UNSAT on cycle: %+v", res.Conflict)
	}
	if len(res.Cycles) == 0 {
		t.Fatal("solver failed to report cycle")
	}
	greedyOptimal(t, reg, roots, false, res)
}
