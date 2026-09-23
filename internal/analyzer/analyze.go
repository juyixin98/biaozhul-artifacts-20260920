package analyzer

import (
	"fmt"
	"sort"
	"strings"
)

// Analyze 对单个 trace 的 span 集合执行关键路径分析。
//
// 返回的 Report 永远非空：即使存在结构性错误（循环、重复 ID、负时长），
// 也会带回完整 Diagnostics；此时 HasError=true，关键路径字段留空（结果不可信）。
func Analyze(traceID string, spans []Span) *Report {
	r := &Report{
		TraceID:   traceID,
		SelfTimes: map[string]int64{},
	}

	// ---- 1. 索引化 + 基础结构校验（错误级问题全部收集后再决定是否继续）----
	byID := map[string]Span{}
	order := []string{}
	for _, s := range spans {
		if _, dup := byID[s.SpanID]; dup {
			addDiag(r, SevError, "DUPLICATE_SPAN_ID", s.SpanID,
				fmt.Sprintf("span_id %q 在同一 trace 中重复出现", s.SpanID))
			continue
		}
		byID[s.SpanID] = s
		order = append(order, s.SpanID)
		if s.EndUs < s.StartUs {
			addDiag(r, SevError, "NEGATIVE_DURATION", s.SpanID,
				fmt.Sprintf("span %q end_us(%d) < start_us(%d)，时长为负", s.Name, s.EndUs, s.StartUs))
		}
	}

	children := map[string][]string{}
	var roots []string
	for _, id := range order {
		s := byID[id]
		if s.ParentID == "" {
			roots = append(roots, id)
			continue
		}
		if _, ok := byID[s.ParentID]; !ok {
			// 缺 span：父引用不存在。不猜测归属，提升为根并告警。
			addDiag(r, SevWarning, "ORPHAN_SPAN", id,
				fmt.Sprintf("span %q 引用的 parent_id %q 不存在，已提升为根处理", s.Name, s.ParentID))
			roots = append(roots, id)
			continue
		}
		children[s.ParentID] = append(children[s.ParentID], id)
	}

	// 循环检测：迭代三色 DFS，避免深层递归。
	color := map[string]int{} // 0 白 1 灰 2 黑
	var stack []frame
	for _, id := range order {
		if color[id] != 0 {
			continue
		}
		stack = append(stack, frame{id: id})
		for len(stack) > 0 {
			top := &stack[len(stack)-1]
			cur := byID[top.id]
			if !top.entered {
				color[cur.SpanID] = 1
				top.entered = true
			}
			if top.ci < len(children[cur.SpanID]) {
				kid := children[cur.SpanID][top.ci]
				top.ci++
				switch color[kid] {
				case 0:
					stack = append(stack, frame{id: kid})
				case 1:
					path := cyclePath(idsOf(stack), kid)
					addDiag(r, SevError, "CYCLE_DETECTED", kid,
						fmt.Sprintf("parent 关系存在循环: %s", strings.Join(path, " -> ")))
				}
				continue
			}
			color[cur.SpanID] = 2
			stack = stack[:len(stack)-1]
		}
	}

	if r.HasError {
		return r // 结构性错误：不做路径计算，HTTP 层按 422 处理
	}
	if len(roots) == 0 {
		addDiag(r, SevError, "EMPTY_TRACE", "", "trace 中没有任何 span")
		return r
	}

	// ---- 2. 选择主根（多根时取 start 最小、平局取 span_id 最小）----
	sort.Slice(roots, func(i, j int) bool {
		a, b := byID[roots[i]], byID[roots[j]]
		if a.StartUs != b.StartUs {
			return a.StartUs < b.StartUs
		}
		return a.SpanID < b.SpanID
	})
	if len(roots) > 1 {
		addDiag(r, SevWarning, "MULTIPLE_ROOTS", roots[0],
			fmt.Sprintf("trace 有 %d 个根（含孤儿提升），以最早开始的 %q 为分析窗口，其余根不进入关键路径",
				len(roots), byID[roots[0]].Name))
	}
	root := byID[roots[0]]
	r.RootID = root.SpanID
	r.RootDurationUs = root.EndUs - root.StartUs

	// ---- 3. 时钟矛盾诊断（扫描归属使用原始区间，不做静默修改）----
	depth := map[string]int{}   // 主树深度
	inMain := map[string]bool{} // 是否属于主根这棵树

	var walk func(id string, clipLo, clipHi int64)
	walk = func(id string, clipLo, clipHi int64) {
		s := byID[id]
		inMain[id] = true
		kids := append([]string{}, children[id]...)
		sort.Slice(kids, func(i, j int) bool {
			a, b := byID[kids[i]], byID[kids[j]]
			if a.StartUs != b.StartUs {
				return a.StartUs < b.StartUs
			}
			return a.SpanID < b.SpanID
		})

		// 3a. 孩子超出父区间 = 时钟矛盾（典型跨机时钟偏差/错误埋点）。
		for _, k := range kids {
			ks := byID[k]
			if ks.StartUs < clipLo || ks.EndUs > clipHi {
				addDiag(r, SevWarning, "CHILD_OUTSIDE_PARENT", k,
					fmt.Sprintf("span %q [%d,%d) 超出父 %q 区间 [%d,%d)，疑似时钟偏差；扫描窗口内的部分仍参与计算",
						ks.Name, ks.StartUs, ks.EndUs, s.Name, clipLo, clipHi))
			}
		}

		// 3b. 同步兄弟重叠：串行依赖模型下两个同步孩子不应同时在跑。
		for i := 0; i < len(kids); i++ {
			if byID[kids[i]].Async {
				continue
			}
			for j := i + 1; j < len(kids); j++ {
				if byID[kids[j]].Async {
					continue
				}
				a, b := byID[kids[i]], byID[kids[j]]
				if a.StartUs < b.EndUs && b.StartUs < a.EndUs {
					addDiag(r, SevWarning, "OVERLAPPING_SYNC_SIBLINGS", a.SpanID,
						fmt.Sprintf("同步子 span %q[%d,%d) 与 %q[%d,%d) 时间重叠，串行依赖下不应并发，疑似时钟偏差；重叠段按较晚结束者归属",
							a.Name, a.StartUs, a.EndUs, b.Name, b.StartUs, b.EndUs))
				}
			}
		}

		for _, k := range kids {
			depth[k] = depth[id] + 1
			klo, khi := max64(byID[k].StartUs, clipLo), min64(byID[k].EndUs, clipHi)
			walk(k, klo, khi)
		}
	}
	depth[root.SpanID] = 0
	walk(root.SpanID, root.StartUs, root.EndUs)

	// ---- 4. 自耗时：父时长 - 全部孩子区间并集（重叠只扣一次），自耗时不为负 ----
	// 注意：自耗时对同步/异步孩子一视同仁（都算“花在孩子上的时间”）；
	// 它与关键路径归属是两个口径——关键路径只被“同步（阻塞）”工作顶起。
	for _, id := range order {
		s := byID[id]
		var raw [][2]int64
		for _, k := range children[id] {
			ks := byID[k]
			lo, hi := max64(ks.StartUs, s.StartUs), min64(ks.EndUs, s.EndUs)
			if hi > lo {
				raw = append(raw, [2]int64{lo, hi})
			}
		}
		cov := unionLength(raw)
		dur := s.EndUs - s.StartUs
		self := dur - cov
		if self < 0 {
			self = 0
		}
		r.SelfTimes[id] = self
		r.SumSpanDurationUs += dur
	}

	// ---- 5. 墙上时间扫描 ----
	// 分析窗口 = [root.Start, max(主树所有 span 的结束))。
	// async 子任务越过 root 结束仍在跑（泄漏的后台任务）时，窗口向后扩展，
	// 越过 root 的那段时间真实地延长了 trace，归属给最深的活动 async 链。
	winLo := root.StartUs
	winHi := root.EndUs
	for id := range inMain {
		if byID[id].EndUs > winHi {
			winHi = byID[id].EndUs
		}
	}
	for id := range inMain {
		s := byID[id]
		if s.Async && s.EndUs > root.EndUs {
			addDiag(r, SevWarning, "ASYNC_TAIL_EXTENDS_TRACE", id,
				fmt.Sprintf("并行子任务 %q 结束于 %d，越过根结束 %d：其尾部真实延长了 trace，落在关键路径上",
					s.Name, s.EndUs, root.EndUs))
		}
	}

	type ev struct {
		t   int64
		del bool
		id  string
	}
	var evs []ev
	evs = append(evs, ev{t: winLo, id: root.SpanID}, ev{t: root.EndUs, del: true, id: root.SpanID})
	for id := range inMain {
		if id == root.SpanID {
			continue
		}
		s := byID[id]
		lo, hi := max64(s.StartUs, winLo), min64(s.EndUs, winHi)
		if hi <= lo {
			continue
		}
		evs = append(evs, ev{t: lo, id: id}, ev{t: hi, del: true, id: id})
	}
	// 半开区间 [start,end)：同一时刻先结束后开始，相邻区间不产生零长重叠。
	sort.SliceStable(evs, func(i, j int) bool {
		if evs[i].t != evs[j].t {
			return evs[i].t < evs[j].t
		}
		if evs[i].del != evs[j].del {
			return evs[i].del
		}
		return evs[i].id < evs[j].id
	})

	// 归属规则（核心）：
	//  1. 窗口内每个时刻，先在“活动的同步 span”中选最深者（同层取结束更晚、平局取 id 小）；
	//  2. 没有任何同步 span 活动时（只可能发生在 root.End 之后的扩展窗口），
	//     才在活动的 async span 中选最深者——即“异步尾部顶长了 trace”；
	//  因此 async 任务与父自身工作并行时，时间归父（父没在等它），不计异步。
	active := map[string]bool{}
	attr := map[string]int64{}
	var firstOrder []string
	prev := winLo
	choose := func() string {
		bestSync, bestAsync := "", ""
		for id := range active {
			if byID[id].Async {
				if bestAsync == "" || deeperLater(id, bestAsync, byID, depth) {
					bestAsync = id
				}
			} else {
				if bestSync == "" || deeperLater(id, bestSync, byID, depth) {
					bestSync = id
				}
			}
		}
		if bestSync != "" {
			return bestSync
		}
		return bestAsync
	}
	for _, e := range evs {
		if e.t > prev {
			if w := choose(); w != "" {
				if _, seen := attr[w]; !seen {
					firstOrder = append(firstOrder, w)
				}
				attr[w] += e.t - prev
			}
		}
		if e.del {
			delete(active, e.id)
		} else {
			active[e.id] = true
		}
		prev = e.t
	}

	var cpTotal int64
	for _, id := range firstOrder {
		s := byID[id]
		r.CriticalPath = append(r.CriticalPath, PathSpan{
			SpanID:       id,
			Name:         s.Name,
			AttributedUs: attr[id],
			Async:        s.Async,
		})
		cpTotal += attr[id]
	}
	r.CriticalPathDurationUs = cpTotal

	// ---- 6. 并行余量：async 任务在“同步信封”（根区间）内覆盖的墙上时间并集 ----
	// 用并集而非逐 span 求和：重叠的并行兄弟只省下一份墙上时间。
	// 越过 root.End 的异步尾部不记余量（它在第 5 步已真实顶长 trace）。
	var asyncCover [][2]int64
	for id := range inMain {
		s := byID[id]
		if !s.Async {
			continue
		}
		lo, hi := max64(s.StartUs, winLo), min64(s.EndUs, root.EndUs)
		if hi > lo {
			asyncCover = append(asyncCover, [2]int64{lo, hi})
		}
	}
	r.ParallelSlackUs = unionLength(asyncCover)

	return r
}

type frame struct {
	id      string
	entered bool
	ci      int
}

func idsOf(st []frame) []string {
	out := make([]string, len(st))
	for i, f := range st {
		out[i] = f.id
	}
	return out
}

// cyclePath 从 DFS 栈中截取循环闭合环。
func cyclePath(stackIDs []string, target string) []string {
	for i, id := range stackIDs {
		if id == target {
			return append(stackIDs[i:], target)
		}
	}
	return append(append([]string{}, stackIDs...), target)
}

// deeperLater 归属竞争比较：深者胜；同深取结束更晚；平局取 span_id 字典序。
func deeperLater(a, b string, byID map[string]Span, depth map[string]int) bool {
	if depth[a] != depth[b] {
		return depth[a] > depth[b]
	}
	if byID[a].EndUs != byID[b].EndUs {
		return byID[a].EndUs > byID[b].EndUs
	}
	return a < b
}

// unionLength 求若干区间并集总长度：先排序合并，重叠部分只计一次。
func unionLength(ivs [][2]int64) int64 {
	if len(ivs) == 0 {
		return 0
	}
	sorted := make([][2]int64, len(ivs))
	copy(sorted, ivs)
	sort.Slice(sorted, func(i, j int) bool {
		if sorted[i][0] != sorted[j][0] {
			return sorted[i][0] < sorted[j][0]
		}
		return sorted[i][1] < sorted[j][1]
	})
	var total int64
	lo, hi := sorted[0][0], sorted[0][1]
	for _, iv := range sorted[1:] {
		if iv[0] <= hi {
			if iv[1] > hi {
				hi = iv[1]
			}
		} else {
			total += hi - lo
			lo, hi = iv[0], iv[1]
		}
	}
	total += hi - lo
	return total
}

func addDiag(r *Report, sev Severity, kind, spanID, msg string) {
	r.Diagnostics = append(r.Diagnostics, Diagnostic{
		Code: sev, Kind: kind, SpanID: spanID, Message: msg,
	})
	if sev == SevError {
		r.HasError = true
	}
}

func min64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}
func max64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}
