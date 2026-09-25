package trace

import (
	"slices"
	"sort"
	"strings"
)

// 额外闭环/不完整原因。
const ReasonMissingParent = "missing-parent"

// buildRevision 依据当前 canonical 图构造一份确定性快照（不分配编号、不落盘）。
func (a *Assembler) buildRevision(st *traceState, traceID string) Revision {
	rev := Revision{
		TraceID:     traceID,
		Closed:      !st.open,
		WatermarkNS: a.watermark,
		EmittedAtNS: a.watermark,
	}

	// span 按到达顺序排列（乱序摄入的“拼装”结果即体现在这里）。
	for _, id := range st.order {
		s := st.canonical[id]
		rev.SpanIDs = append(rev.SpanIDs, id)
		rev.Spans = append(rev.Spans, *s)
		if s.ParentSpanID == "" {
			rev.RootSpanIDs = append(rev.RootSpanIDs, id)
		} else if _, ok := st.canonical[s.ParentSpanID]; !ok {
			rev.MissingParents = append(rev.MissingParents, MissingParent{
				ChildID: id, ParentID: s.ParentSpanID,
			})
		}
	}

	rev.Cycles = detectCycles(st)
	rev.Skew = detectSkew(st, a.cfg.SkewToleranceNS)
	rev.Conflicts = slices.Clone(st.conflicts)

	switch {
	case len(rev.RootSpanIDs) > 1:
		rev.Reasons = append(rev.Reasons, ReasonMultiRoot)
	case len(rev.RootSpanIDs) == 0:
		// 没有根（缺根）本质上也是存在悬空父边。
		rev.Reasons = append(rev.Reasons, "missing-root")
	}
	if len(rev.MissingParents) > 0 {
		rev.Reasons = append(rev.Reasons, ReasonMissingParent)
	}
	if len(rev.Cycles) > 0 {
		rev.Reasons = append(rev.Reasons, ReasonCycle)
	}
	rev.Complete = len(rev.MissingParents) == 0 &&
		len(rev.RootSpanIDs) == 1 && len(rev.Cycles) == 0
	if rev.Complete {
		rev.Reasons = append(rev.Reasons, ReasonComplete)
	}
	return rev
}

// detectCycles 沿父指针行走，找出父链中的所有循环。
// 单父图中每个连通分量至多一个环；从每个节点各走一遍以保证不遗漏，
// 环做“最小 span_id 旋转到首位”的归一化后去重。
func detectCycles(st *traceState) [][]string {
	seen := map[string]bool{}
	var cycles [][]string
	for _, start := range st.order {
		pos := map[string]int{}
		var stack []string
		cur := start
		for cur != "" {
			node, ok := st.canonical[cur]
			if !ok {
				break // 父缺失，不成环
			}
			if idx, ok := pos[cur]; ok {
				cyc := normalizeCycle(stack[idx:])
				key := strings.Join(cyc, "|")
				if !seen[key] {
					seen[key] = true
					cycles = append(cycles, cyc)
				}
				break
			}
			pos[cur] = len(stack)
			stack = append(stack, cur)
			cur = node.ParentSpanID
		}
	}
	sort.Slice(cycles, func(i, j int) bool {
		return strings.Join(cycles[i], "|") < strings.Join(cycles[j], "|")
	})
	return cycles
}

func normalizeCycle(c []string) []string {
	minIdx := 0
	for i := 1; i < len(c); i++ {
		if c[i] < c[minIdx] {
			minIdx = i
		}
	}
	out := make([]string, 0, len(c))
	out = append(out, c[minIdx:]...)
	out = append(out, c[:minIdx]...)
	return out
}

// detectSkew 仅依据父子本地时间戳给出信息性告警；绝不参与结构/因果判定。
func detectSkew(st *traceState, tolerance int64) []SkewEvent {
	var out []SkewEvent
	for _, id := range st.order {
		child := st.canonical[id]
		if child.ParentSpanID == "" {
			continue
		}
		parent, ok := st.canonical[child.ParentSpanID]
		if !ok {
			continue
		}
		ps := spanStartNS(parent)
		pe := ps + parent.DurationNanos
		cs := spanStartNS(child)
		ce := cs + child.DurationNanos
		if cs < ps-tolerance {
			out = append(out, SkewEvent{
				ParentSpanID: parent.SpanID, ChildSpanID: child.SpanID,
				ParentStart: ps, ParentEnd: pe, ChildStart: cs, ChildEnd: ce,
				DeltaNS: cs - ps, Kind: "starts-before-parent",
			})
		}
		if ce > pe+tolerance {
			out = append(out, SkewEvent{
				ParentSpanID: parent.SpanID, ChildSpanID: child.SpanID,
				ParentStart: ps, ParentEnd: pe, ChildStart: cs, ChildEnd: ce,
				DeltaNS: ce - pe, Kind: "ends-after-parent",
			})
		}
	}
	return out
}

func cloneRevision(r Revision) Revision {
	cp := r
	cp.SpanIDs = slices.Clone(r.SpanIDs)
	cp.RootSpanIDs = slices.Clone(r.RootSpanIDs)
	cp.Reasons = slices.Clone(r.Reasons)
	cp.Spans = make([]Span, len(r.Spans))
	for i, s := range r.Spans {
		s.Attributes = cloneAttrs(s.Attributes)
		cp.Spans[i] = s
	}
	cp.Conflicts = make([]Conflict, len(r.Conflicts))
	for i, c := range r.Conflicts {
		c.Fields = slices.Clone(c.Fields)
		c.Duplicate.Attributes = cloneAttrs(c.Duplicate.Attributes)
		cp.Conflicts[i] = c
	}
	cp.MissingParents = slices.Clone(r.MissingParents)
	cp.Cycles = make([][]string, len(r.Cycles))
	for i, c := range r.Cycles {
		cp.Cycles[i] = slices.Clone(c)
	}
	cp.Skew = slices.Clone(r.Skew)
	return cp
}

func cloneAttrs(m map[string]string) map[string]string {
	if m == nil {
		return nil
	}
	cp := make(map[string]string, len(m))
	for k, v := range m {
		cp[k] = v
	}
	return cp
}

// GetTrace 返回某 trace 的当前视图：打开中为实时视图（revision 为已关闭次数），
// 已关闭为最近一次修订快照。
func (a *Assembler) GetTrace(traceID string) (Revision, bool, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	st, ok := a.traces[traceID]
	if !ok {
		return Revision{}, false, false
	}
	if st.open {
		rev := a.buildRevision(st, traceID)
		rev.Revision = st.revision
		return rev, true, true
	}
	return *st.lastRev, true, false
}

// GetRevision 返回某 trace 的指定编号修订（编号从 1 开始）。
func (a *Assembler) GetRevision(traceID string, n int64) (Revision, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	st, ok := a.traces[traceID]
	if !ok || n < 1 || int(n) > len(st.history) {
		return Revision{}, false
	}
	return st.history[int(n)-1], true
}

// TraceSummary 是列表项。
type TraceSummary struct {
	TraceID       string `json:"trace_id"`
	Open          bool   `json:"open"`
	Revision      int64  `json:"revision"`
	SpanCount     int    `json:"span_count"`
	Complete      bool   `json:"complete"`
	LastEmittedNS int64  `json:"last_emitted_ns,omitempty"`
}

// ListTraces 按 trace_id 排序列出全部 trace。
func (a *Assembler) ListTraces() []TraceSummary {
	a.mu.Lock()
	defer a.mu.Unlock()
	ids := make([]string, 0, len(a.traces))
	for id := range a.traces {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	out := make([]TraceSummary, 0, len(ids))
	for _, id := range ids {
		st := a.traces[id]
		s := TraceSummary{TraceID: id, Open: st.open, Revision: st.revision, SpanCount: len(st.order)}
		if st.lastRev != nil {
			s.Complete = st.lastRev.Complete
			s.LastEmittedNS = st.lastRev.EmittedAtNS
		}
		out = append(out, s)
	}
	return out
}

// ---- 重放接口：供持久层按 WAL 顺序重建状态，不产生额外 WAL 追加 ----

// RestoreApplySpan 重放一个已持久化的摄入 span（含重开语义，但不触发关闭）。
func (a *Assembler) RestoreApplySpan(s Span) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.applyOne(&s)
}

// RestoreSweep 重放批边界的超时扫描动作（不产生 WAL）。
func (a *Assembler) RestoreSweep() []Revision {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.sweepLocked()
}

// RestoreAdvanceWatermark 重放一次水位推进，可能重算并落盘关闭修订，
// 但不会向 WAL 追加记录。
func (a *Assembler) RestoreAdvanceWatermark(ns int64) []Revision {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.advanceWatermarkLocked(ns)
}
