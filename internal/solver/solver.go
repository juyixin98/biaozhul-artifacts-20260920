// Package solver 是确定性的依赖版本约束求解器。
//
// 策略：每个包在最终解中只允许存在一个版本（flat resolution）。
// 求解过程是带回溯的贪心搜索——总是为当前激活的包尝试“满足全部约束的最新版本”，
// 失败则回退到更旧的候选；整个过程中每一步决策与冲突都记录在 Event 轨迹里，
// 无解时给出可解释的依赖链 Conflict。
package solver

import (
	"fmt"
	"sort"
	"strings"

	"depsolver/internal/model"
	"depsolver/internal/registry"
	"depsolver/internal/semver"
)

// Options 控制求解行为。
type Options struct {
	// IncludePrereleases 关闭简化 npm 预发布门控：为 true 时所有预发布版本都可被选中。
	IncludePrereleases bool
}

// Hop 是解释链上的一跳。
type Hop struct {
	Package    string `json:"package"`
	Version    string `json:"version,omitempty"`
	RequiredBy string `json:"requiredBy,omitempty"`
	Constraint string `json:"constraint,omitempty"`
	Reason     string `json:"reason,omitempty"`
}

// Event 是求解轨迹上的一个决策或观察，保证顺序与求解过程一致。
type Event struct {
	Kind       string      `json:"kind"`
	Depth      int         `json:"depth"`
	Package    string      `json:"package,omitempty"`
	Version    string      `json:"version,omitempty"`
	Constraint string      `json:"constraint,omitempty"`
	RequiredBy string      `json:"requiredBy,omitempty"`
	Rejected   []Rejection `json:"rejected,omitempty"`
	Reason     string      `json:"reason,omitempty"`
}

// Rejection 是候选版本被跳过的原因。
type Rejection struct {
	Version string `json:"version"`
	Reason  string `json:"reason"`
}

// Conflict 是无解时的结构化解释，Causes 给出导致冲突的依赖链。
type Conflict struct {
	Type     string      `json:"type"`
	Package  string      `json:"package,omitempty"`
	Message  string      `json:"message"`
	Chain    []Hop       `json:"chain,omitempty"`
	Rejected []Rejection `json:"rejected,omitempty"`
	// Cause 是更深一层的失败解释（回溯冒泡链），最终返回的是最深的原始冲突。
	Cause *Conflict `json:"cause,omitempty"`
}

// Selection 是最终选中的一个包版本。
type Selection struct {
	Package string `json:"package"`
	Version string `json:"version"`
}

// Cycle 是求解后在选中图里发现的一个循环（名字升序的有序环）。
type Cycle struct {
	Packages []string `json:"packages"`
}

// Result 是求解结果；SAT 时 Satisfiable=true，UNSAT 时 Conflict 非空。
type Result struct {
	Satisfiable bool        `json:"satisfiable"`
	Selections  []Selection `json:"selections,omitempty"`
	Cycles      []Cycle     `json:"cycles,omitempty"`
	Conflict    *Conflict   `json:"conflict,omitempty"`
	Trace       []Event     `json:"trace"`
	Stats       Stats       `json:"stats"`
}

// Stats 汇总搜索工作量。
type Stats struct {
	Decisions        int `json:"decisions"`
	Backtracks       int `json:"backtracks"`
	Conflicts        int `json:"conflicts"`
	RejectedVersions int `json:"rejected_versions"`
}

// req 是某包在当前搜索分支上累积的一条需求。
type req struct {
	name       string
	constraint semver.Constraint
	raw        string
	requiredBy string // 引入该需求的包名；根依赖为 ""
}

// choice 是一个待决策（激活）的包。
type choice struct {
	name string
	via  *req // 首次引入它的需求，用于解释链
}

type solver struct {
	reg     *registry.Registry
	opts    Options
	trace   []Event
	stats   Stats
	reqs    map[string][]req
	chosen  map[string]registry.ParsedVersion
	pending []choice
	inQueue map[string]struct{}
	// activeAtDepth 记录每个搜索深度上新激活的包，用于失败时构造解释链
	activated []string
}

// Solve 在给定注册表上对 roots 求一组满足所有约束的确定版本集。
// roots 为根工程的直接依赖；注册表与缓存/网络无关，完全由调用方提供。
func Solve(reg *registry.Registry, roots []model.Dependency, opts Options) (*Result, error) {
	s := &solver{
		reg:     reg,
		opts:    opts,
		reqs:    map[string][]req{},
		chosen:  map[string]registry.ParsedVersion{},
		inQueue: map[string]struct{}{},
	}
	// 根依赖按名字排序，保证起点确定。
	sortedRoots := append([]model.Dependency(nil), roots...)
	sort.SliceStable(sortedRoots, func(i, j int) bool { return sortedRoots[i].Name < sortedRoots[j].Name })
	for _, d := range sortedRoots {
		if !registry.ValidName(d.Name) {
			return nil, fmt.Errorf("invalid root dependency name %q", d.Name)
		}
		c, err := semver.ParseConstraint(d.Constraint)
		if err != nil {
			return nil, fmt.Errorf("root dependency %q: %w", d.Name, err)
		}
		s.trace = append(s.trace, Event{
			Kind: "add-requirement", Depth: 0, Package: d.Name,
			Constraint: d.Constraint, Reason: "root",
		})
		if cf := s.addReq(req{name: d.Name, constraint: c, raw: d.Constraint}); cf != nil {
			s.stats.Conflicts++
			s.trace = append(s.trace, Event{
				Kind: "conflict", Depth: 0, Package: d.Name, Reason: cf.Message,
			})
			return s.fail(cf), nil
		}
	}
	if cf := s.dfs(0); cf != nil {
		return s.fail(cf), nil
	}
	return s.success(), nil
}

// snapshot 是单个搜索决策点的可恢复状态。
type snapshot struct {
	reqs      map[string][]req
	chosen    map[string]registry.ParsedVersion
	pending   []choice
	inQueue   map[string]struct{}
	activated []string
	traceLen  int
}

func (s *solver) save(traceLen int) snapshot {
	return snapshot{
		reqs:      cloneReqs(s.reqs),
		chosen:    cloneChosen(s.chosen),
		pending:   append([]choice(nil), s.pending...),
		inQueue:   cloneSet(s.inQueue),
		activated: append([]string(nil), s.activated...),
		traceLen:  traceLen,
	}
}

func (s *solver) restore(snap snapshot) {
	s.reqs = snap.reqs
	s.chosen = snap.chosen
	s.pending = snap.pending
	s.inQueue = snap.inQueue
	s.activated = snap.activated
}

func cloneReqs(m map[string][]req) map[string][]req {
	out := make(map[string][]req, len(m))
	for k, v := range m {
		out[k] = append([]req(nil), v...)
	}
	return out
}

func cloneChosen(m map[string]registry.ParsedVersion) map[string]registry.ParsedVersion {
	out := make(map[string]registry.ParsedVersion, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

func cloneSet(m map[string]struct{}) map[string]struct{} {
	out := make(map[string]struct{}, len(m))
	for k := range m {
		out[k] = struct{}{}
	}
	return out
}

// dfs 在当前分支上不断取出 pending 中的包做决策，直到成功或返回冲突。
func (s *solver) dfs(depth int) *Conflict {
	for len(s.pending) > 0 {
		c := s.pending[0]
		s.pending = s.pending[1:]
		delete(s.inQueue, c.name)

		pkg := s.reg.Package(c.name)
		if pkg == nil || len(pkg.Versions) == 0 {
			cf := s.makeUnknownConflict(c)
			s.stats.Conflicts++
			s.trace = append(s.trace, Event{
				Kind: "conflict", Depth: depth, Package: c.name,
				Reason: cf.Message,
			})
			return cf
		}

		candidates, rejected := s.viable(pkg)
		s.stats.RejectedVersions += len(rejected)
		s.trace = append(s.trace, Event{
			Kind: "select-package", Depth: depth, Package: c.name,
			Reason:   fmt.Sprintf("%d viable of %d versions", len(candidates), len(pkg.Versions)),
			Rejected: rejected,
		})
		if len(candidates) == 0 {
			cf := s.makeNoCandidateConflict(c, pkg, rejected)
			s.stats.Conflicts++
			s.trace = append(s.trace, Event{
				Kind: "conflict", Depth: depth, Package: c.name,
				Rejected: rejected, Reason: cf.Message,
			})
			return cf
		}

		var lastCF *Conflict
		for idx, cand := range candidates {
			snap := s.save(len(s.trace))
			s.trace = append(s.trace, Event{
				Kind: "try", Depth: depth, Package: c.name, Version: cand.Raw,
				Reason: fmt.Sprintf("candidate %d/%d", idx+1, len(candidates)),
			})
			s.stats.Decisions++
			cf := s.activate(c, cand, depth)
			if cf == nil {
				s.activated = append(s.activated, c.name)
				if deeper := s.dfs(depth + 1); deeper == nil {
					return nil
				} else {
					cf = deeper
				}
			}
			s.stats.Backtracks++
			s.stats.Conflicts++
			s.restore(snap)
			s.trace = append(s.trace, Event{
				Kind: "backtrack", Depth: depth, Package: c.name, Version: cand.Raw,
				Reason: shortConflict(cf),
			})
			// 冒泡：最深的原始冲突保持为主解释，本层尝试作为附加上下文。
			lastCF = s.bubbleConflict(c, cand.Raw, cf)
		}
		return lastCF
	}
	return nil
}

func shortConflict(cf *Conflict) string {
	if cf == nil {
		return "subtree unsatisfiable"
	}
	return cf.Type + ": " + cf.Message
}

// viable 过滤并返回当前包可尝试的候选（降序），同时记录每个落选版本原因。
func (s *solver) viable(pkg *registry.PackageEntry) ([]registry.ParsedVersion, []Rejection) {
	reqs := s.reqs[pkg.Name]
	var out []registry.ParsedVersion
	var rejected []Rejection
	for _, pv := range pkg.Versions {
		ok := true
		var why string
		if pv.V.IsPrerelease() && !s.opts.IncludePrereleases && !s.prereleaseAllowed(pkg.Name, pv.V) {
			ok = false
			why = "prerelease not allowed by gate (set includePrereleases or mention this X.Y.Z in a constraint)"
		}
		if ok {
			for _, r := range reqs {
				if !r.constraint.Satisfy(pv.V, s.opts.IncludePrereleases) {
					ok = false
					why = fmt.Sprintf("does not satisfy %s (required by %s)", r.raw, orRoot(r.requiredBy))
					break
				}
			}
		}
		if ok {
			out = append(out, pv)
		} else {
			rejected = append(rejected, Rejection{Version: pv.Raw, Reason: why})
		}
	}
	return out, rejected
}

// prereleaseAllowed 实现简化 npm 门控：该包任一需求显式提及同一 X.Y.Z 预发布基元即放行。
func (s *solver) prereleaseAllowed(name string, v semver.Version) bool {
	for _, r := range s.reqs[name] {
		if r.constraint.MentionsPrerelease(v.Key()) {
			return true
		}
	}
	return false
}

// activate 选定 cand 并把它的依赖需求并入分支；返回冲突则本次尝试失败。
func (s *solver) activate(c choice, cand registry.ParsedVersion, depth int) *Conflict {
	s.chosen[c.name] = cand
	s.trace = append(s.trace, Event{
		Kind: "activate", Depth: depth, Package: c.name, Version: cand.Raw,
	})
	deps := append([]registry.ParsedDep(nil), cand.Deps...)
	sort.SliceStable(deps, func(i, j int) bool { return deps[i].Name < deps[j].Name })
	for _, d := range deps {
		s.trace = append(s.trace, Event{
			Kind: "add-requirement", Depth: depth, Package: d.Name,
			Constraint: d.Raw, RequiredBy: c.name,
		})
		if cf := s.addReq(req{name: d.Name, constraint: d.Constraint, raw: d.Raw, requiredBy: c.name}); cf != nil {
			return cf
		}
	}
	return nil
}

// addReq 并入一条需求。若包已有选定版本且不满足，或新增需求与既有需求无可行交集，
// 返回解释链冲突；否则把新出现的包放入 pending。
func (s *solver) addReq(r req) *Conflict {
	pkg := s.reg.Package(r.name)
	if pkg == nil || len(pkg.Versions) == 0 {
		return s.makeUnknownConflict(choice{name: r.name, via: &r})
	}
	existing := s.reqs[r.name]
	if cur, ok := s.chosen[r.name]; ok {
		if !r.constraint.Satisfy(cur.V, s.opts.IncludePrereleases) {
			return s.makeClashConflict(r, existing, cur)
		}
	}
	// 交集预检：存在某个版本同时满足全部需求（含新需求）且通过预发布门控。
	all := append(append([]req(nil), existing...), r)
	if feasible, rejected := s.feasibleWithReasons(pkg, all); !feasible {
		return s.makeIntersectionConflict(r, existing, rejected)
	}
	s.reqs[r.name] = all
	if _, chosen := s.chosen[r.name]; !chosen {
		if _, queued := s.inQueue[r.name]; !queued {
			s.inQueue[r.name] = struct{}{}
			s.pending = append(s.pending, choice{name: r.name, via: &r})
		}
	}
	return nil
}

// feasibleWithReasons 在给定需求集上判断是否存在可行版本，
// 并为每个被排除的版本返回具体原因（版本输入已降序）。
func (s *solver) feasibleWithReasons(pkg *registry.PackageEntry, reqs []req) (bool, []Rejection) {
	var rejected []Rejection
	for _, pv := range pkg.Versions {
		if pv.V.IsPrerelease() && !s.opts.IncludePrereleases && !s.reqMentions(reqs, pv.V.Key()) {
			rejected = append(rejected, Rejection{Version: pv.Raw,
				Reason: "prerelease not allowed by gate (set includePrereleases or mention this X.Y.Z in a constraint)"})
			continue
		}
		bad := ""
		for _, r := range reqs {
			if !r.constraint.Satisfy(pv.V, s.opts.IncludePrereleases) {
				bad = fmt.Sprintf("does not satisfy %s (required by %s)", r.raw, orRoot(r.requiredBy))
				break
			}
		}
		if bad != "" {
			rejected = append(rejected, Rejection{Version: pv.Raw, Reason: bad})
		}
	}
	return len(rejected) < len(pkg.Versions), rejected
}

func (s *solver) reqMentions(reqs []req, key [3]int64) bool {
	for _, r := range reqs {
		if r.constraint.MentionsPrerelease(key) {
			return true
		}
	}
	return false
}

func orRoot(by string) string {
	if by == "" {
		return "root"
	}
	return by
}

// ---- 冲突构造 ----

func (s *solver) chainFor(r req) []Hop {
	var chain []Hop
	cur := r
	for {
		chain = append([]Hop{{
			Package:    cur.name,
			Constraint: cur.raw,
			RequiredBy: orRoot(cur.requiredBy),
		}}, chain...)
		if cur.requiredBy == "" {
			break
		}
		parent, ok := s.chosen[cur.requiredBy]
		if !ok {
			break
		}
		next, ok := findDep(parent.Deps, cur.name)
		if !ok {
			chain = append([]Hop{{Package: cur.requiredBy, Version: parent.Raw}}, chain...)
			break
		}
		cur = req{name: cur.requiredBy, constraint: next.Constraint, raw: next.Raw, requiredBy: s.requiredByOf(cur.requiredBy)}
	}
	return chain
}

func (s *solver) requiredByOf(name string) string {
	for _, r := range s.reqs[name] {
		return r.requiredBy
	}
	return ""
}

func findDep(deps []registry.ParsedDep, name string) (registry.ParsedDep, bool) {
	for _, d := range deps {
		if d.Name == name {
			return d, true
		}
	}
	return registry.ParsedDep{}, false
}

func (s *solver) makeUnknownConflict(c choice) *Conflict {
	cf := &Conflict{
		Type:    "unknown-package",
		Package: c.name,
		Message: fmt.Sprintf("package %q has no versions available in the registry", c.name),
	}
	if c.via != nil {
		cf.Chain = s.chainFor(*c.via)
	}
	return cf
}

func (s *solver) makeClashConflict(r req, existing []req, cur registry.ParsedVersion) *Conflict {
	chain := s.chainFor(r)
	chain = append(chain, Hop{
		Package: r.name, Version: cur.Raw,
		Reason: fmt.Sprintf("already selected %s, cannot also satisfy %s", cur.Raw, r.raw),
	})
	var reqList []string
	for _, e := range existing {
		reqList = append(reqList, fmt.Sprintf("%s<-by %s", e.raw, orRoot(e.requiredBy)))
	}
	return &Conflict{
		Type: "constraint-clash", Package: r.name,
		Message:  fmt.Sprintf("selected %s of %q but %s requires %s", cur.Raw, r.name, orRoot(r.requiredBy), r.raw),
		Chain:    chain,
		Rejected: []Rejection{{Version: cur.Raw, Reason: "incompatible with new requirement " + r.raw}},
	}
}

func (s *solver) makeIntersectionConflict(r req, existing []req, rejected []Rejection) *Conflict {
	var reqList []string
	for _, e := range existing {
		reqList = append(reqList, fmt.Sprintf("%s (by %s)", e.raw, orRoot(e.requiredBy)))
	}
	reqList = append(reqList, fmt.Sprintf("%s (by %s)", r.raw, orRoot(r.requiredBy)))
	return &Conflict{
		Type: "constraint-clash", Package: r.name,
		Message:  fmt.Sprintf("no version of %q satisfies all of: %s", r.name, strings.Join(reqList, "; ")),
		Chain:    s.chainFor(r),
		Rejected: rejected,
	}
}

func (s *solver) makeNoCandidateConflict(c choice, pkg *registry.PackageEntry, rejected []Rejection) *Conflict {
	reqs := s.reqs[c.name]
	var reqList []string
	for _, r := range reqs {
		reqList = append(reqList, fmt.Sprintf("%s (by %s)", r.raw, orRoot(r.requiredBy)))
	}
	cf := &Conflict{
		Type: "no-candidate", Package: c.name,
		Message:  fmt.Sprintf("no available version of %q satisfies: %s", c.name, strings.Join(reqList, "; ")),
		Rejected: rejected,
	}
	if c.via != nil {
		cf.Chain = s.chainFor(*c.via)
	}
	return cf
}

// bubbleConflict 在回溯冒泡时保留最深的原始冲突为主解释，
// 并把“上层哪个候选因此失败”作为一层 Cause 上下文附挂（去重，避免链膨胀）。
func (s *solver) bubbleConflict(c choice, triedVersion string, deeper *Conflict) *Conflict {
	layer := &Conflict{
		Type:     "candidate-rejected",
		Package:  c.name,
		Message:  fmt.Sprintf("candidate %s@%s rejected", c.name, triedVersion),
		Rejected: []Rejection{{Version: triedVersion, Reason: deeper.Type + ": " + deeper.Message}},
	}
	root := deeper
	for root.Cause != nil {
		root = root.Cause
	}
	// 去重：同一包+版本的冒泡层只保留一次
	if !hasLayer(root.Cause, c.name, triedVersion) {
		root.Cause = layer
	}
	return deeper
}

func hasLayer(cf *Conflict, pkg, version string) bool {
	for cf != nil {
		if cf.Package == pkg && len(cf.Rejected) > 0 && cf.Rejected[0].Version == version {
			return true
		}
		cf = cf.Cause
	}
	return false
}

// ---- 结果整理 ----

func (s *solver) fail(cf *Conflict) *Result {
	return &Result{
		Satisfiable: false,
		Conflict:    cf,
		Trace:       s.trace,
		Stats:       s.stats,
	}
}

func (s *solver) success() *Result {
	sels := make([]Selection, 0, len(s.chosen))
	for name, pv := range s.chosen {
		sels = append(sels, Selection{Package: name, Version: pv.Raw})
	}
	sort.Slice(sels, func(i, j int) bool { return sels[i].Package < sels[j].Package })
	return &Result{
		Satisfiable: true,
		Selections:  sels,
		Cycles:      findCycles(s.chosen),
		Trace:       s.trace,
		Stats:       s.stats,
	}
}

// findCycles 在选中图上用确定性 DFS 寻找一组覆盖所有环的简单环。
// 邻居按名字升序访问，输出环做旋转归一化并去重。
func findCycles(chosen map[string]registry.ParsedVersion) []Cycle {
	adj := map[string][]string{}
	names := make([]string, 0, len(chosen))
	for n, pv := range chosen {
		names = append(names, n)
		for _, d := range pv.Deps {
			if _, ok := chosen[d.Name]; ok {
				adj[n] = append(adj[n], d.Name)
			}
		}
		sort.Strings(adj[n])
	}
	sort.Strings(names)

	const (
		white = 0
		gray  = 1
		black = 2
	)
	color := map[string]int{}
	var stack []string
	onStack := map[string]bool{}
	seen := map[string]struct{}{}
	var cycles []Cycle

	var dfs func(n string)
	dfs = func(n string) {
		color[n] = gray
		onStack[n] = true
		stack = append(stack, n)
		for _, m := range adj[n] {
			if color[m] == white {
				dfs(m)
			} else if onStack[m] {
				var cyc []string
				for i := len(stack) - 1; i >= 0; i-- {
					cyc = append([]string{stack[i]}, cyc...)
					if stack[i] == m {
						break
					}
				}
				key := normalizeCycle(cyc)
				if _, dup := seen[key]; dup {
					continue
				}
				seen[key] = struct{}{}
				cycles = append(cycles, Cycle{Packages: cyc})
			}
		}
		stack = stack[:len(stack)-1]
		onStack[n] = false
		color[n] = black
	}
	for _, n := range names {
		if color[n] == white {
			dfs(n)
		}
	}
	sort.Slice(cycles, func(i, j int) bool { return normalizeCycle(cycles[i].Packages) < normalizeCycle(cycles[j].Packages) })
	return cycles
}

func normalizeCycle(c []string) string {
	if len(c) == 0 {
		return ""
	}
	minIdx := 0
	for i := 1; i < len(c); i++ {
		if c[i] < c[minIdx] {
			minIdx = i
		}
	}
	rot := append(append([]string{}, c[minIdx:]...), c[:minIdx]...)
	return strings.Join(rot, ">")
}
