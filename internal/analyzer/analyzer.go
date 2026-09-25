// Package analyzer computes per-span self time, the trace critical
// path over a span DAG, and clock/structural diagnostics.
//
// # Model
//
// Each span declares whether the parent waits for it:
//
//   - sync  child: the parent blocks. Its critical contribution is
//     counted fully, and sync children of one parent must not overlap.
//   - async child: a parallel subtask the parent does NOT wait for.
//     Only the single longest async subtree can extend the critical
//     path; async children may legally overlap each other and the
//     parent's sync work.
//
// # Self time
//
// Self time = span duration MINUS the union of the time intervals the
// span spends waiting on ANY child (sync or async). Intervals are
// clipped to the span's own [start,end] and merged, so overlapped /
// parallel children are counted at most once. We never sum child
// durations directly.
//
// # Critical path
//
// For span s (computed bottom-up over the DAG):
//
//	C(s) = self(s) + Σ C(c) for sync children c
//	            + max C(c) over async children c (omitted if none)
//
// i.e. sync work serializes (sum), parallel work only contributes its
// longest branch (max). The trace critical path is C(root) and the
// ordered path is: root, then each sync chain, then the longest async
// subtree(s) at every level.
package analyzer

import (
	"math"
	"sort"

	"cpathtrace/internal/model"
)

// Diagnostic severities.
const (
	SeverityWarning = "warning"
	SeverityError   = "error"
)

// Diagnostic codes.
const (
	// CodeNegativeDuration: end_time < start_time (impossible clock).
	CodeNegativeDuration = "NEGATIVE_DURATION"
	// CodeZeroDuration: instant span; usually a data problem.
	CodeZeroDuration = "ZERO_DURATION"
	// CodeMissingParent: parent id references a span not in the trace.
	CodeMissingParent = "MISSING_PARENT"
	// CodeSyncOverlap: two sync children of the same parent overlap
	// in wall-clock time; sync work must serialize.
	CodeSyncOverlap = "SYNC_CHILD_OVERLAP"
	// CodeChildOutsideParent: child interval reaches outside the
	// parent interval (clock skew / bad instrumentation).
	CodeChildOutsideParent = "CHILD_OUTSIDE_PARENT"
	// CodeCycle: parent edges contain a cycle, so no DAG exists.
	CodeCycle = "CYCLE"
	// CodeNoRoot: every span has a parent (pure cycle): no entry point.
	CodeNoRoot = "NO_ROOT"
	// CodeMultipleRoots: several spans act as roots; one is chosen.
	CodeMultipleRoots = "MULTIPLE_ROOTS"
	// CodeCriticalExceedsDuration: modeled critical time is longer
	// than the span's wall-clock duration — usually a mislabeled
	// async/sync relation or skewed clocks.
	CodeCriticalExceedsDuration = "CRITICAL_EXCEEDS_DURATION"
)

// Diagnostic is one clock-contradiction or structural finding.
type Diagnostic struct {
	Code     string `json:"code"`
	Severity string `json:"severity"`
	SpanID   string `json:"span_id,omitempty"`
	Message  string `json:"message"`
}

// SpanTime is the per-span timing row.
type SpanTime struct {
	SpanID       string `json:"span_id"`
	Name         string `json:"name"`
	Relation     string `json:"relation"`
	Duration     int64  `json:"duration"`
	SelfTime     int64  `json:"self_time"`
	CriticalTime *int64 `json:"critical_time"`
}

// PathEntry is one span on the ordered critical path.
type PathEntry struct {
	SpanID       string `json:"span_id"`
	Name         string `json:"name"`
	Relation     string `json:"relation"`
	SelfTime     int64  `json:"self_time"`
	CriticalTime int64  `json:"critical_time"`
	Reason       string `json:"reason"`
}

// Result is the full analysis output.
type Result struct {
	TraceID              string       `json:"trace_id"`
	RootSpanID           string       `json:"root_span_id"`
	TraceDuration        *int64       `json:"trace_duration"`
	CriticalPathDuration *int64       `json:"critical_path_duration"`
	Ratio                float64      `json:"critical_to_wall_ratio"`
	CriticalPath         []PathEntry  `json:"critical_path"`
	SpanTimes            []SpanTime   `json:"span_times"`
	Diagnostics          []Diagnostic `json:"diagnostics"`
}

type node struct {
	span     model.Span
	children []*node
}

type analyzer struct {
	nodes map[string]*node
	order []string // span ids sorted by (start, span id): deterministic iteration
	diags []Diagnostic
}

// Analyze runs the full analysis on one trace. It never returns an
// error: malformed input is reported through Diagnostics, and numeric
// fields that cannot be computed are null.
func Analyze(trace model.Trace) *Result {
	a := &analyzer{nodes: map[string]*node{}}

	for i := range trace.Spans {
		s := trace.Spans[i]
		if _, dup := a.nodes[s.SpanID]; dup {
			// Ingestion rejects duplicates; stay deterministic if it
			// does happen: keep the first occurrence.
			continue
		}
		a.nodes[s.SpanID] = &node{span: s}
		a.order = append(a.order, s.SpanID)
	}
	sort.Slice(a.order, func(i, j int) bool {
		x, y := a.nodes[a.order[i]].span, a.nodes[a.order[j]].span
		if x.StartTime != y.StartTime {
			return x.StartTime < y.StartTime
		}
		return x.SpanID < y.SpanID
	})

	// Basic duration checks.
	for _, id := range a.order {
		s := a.nodes[id].span
		switch {
		case s.Duration() < 0:
			a.diags = append(a.diags, Diagnostic{
				Code: CodeNegativeDuration, Severity: SeverityWarning, SpanID: id,
				Message: "end_time is before start_time; clock skew or bad instrumentation",
			})
		case s.Duration() == 0:
			a.diags = append(a.diags, Diagnostic{
				Code: CodeZeroDuration, Severity: SeverityWarning, SpanID: id,
				Message: "zero-duration span",
			})
		}
	}

	// Wire children; flag missing parents.
	var trueRoots, orphanRoots []string
	for _, id := range a.order {
		n := a.nodes[id]
		pid := n.span.ParentSpanID
		if pid == "" {
			trueRoots = append(trueRoots, id)
			continue
		}
		p, ok := a.nodes[pid]
		if !ok {
			a.diags = append(a.diags, Diagnostic{
				Code: CodeMissingParent, Severity: SeverityWarning, SpanID: id,
				Message: "parent_span_id " + pid + " is not present in the trace; treating span as an orphan root",
			})
			orphanRoots = append(orphanRoots, id)
			continue
		}
		p.children = insertChildSorted(p.children, n)
	}

	// Cycle detection over child edges (3-color DFS).
	cyclePath := a.findCycle()

	// Choose the entry span: prefer genuine roots (empty parent) over
	// dangling orphans; within a group take earliest start, then id.
	candidates := trueRoots
	if len(candidates) == 0 {
		candidates = orphanRoots
	}
	pickRoot := func(ids []string) string {
		best := ids[0]
		for _, id := range ids[1:] {
			s, b := a.nodes[id].span, a.nodes[best].span
			if s.StartTime < b.StartTime || (s.StartTime == b.StartTime && s.SpanID < b.SpanID) {
				best = id
			}
		}
		return best
	}
	rootID := ""
	if len(candidates) > 0 {
		rootID = pickRoot(candidates)
	}
	if len(trueRoots) > 1 {
		a.diags = append(a.diags, Diagnostic{
			Code: CodeMultipleRoots, Severity: SeverityWarning, SpanID: rootID,
			Message: "trace has multiple root spans; analysis uses " + rootID,
		})
	}
	if len(candidates) == 0 {
		a.diags = append(a.diags, Diagnostic{
			Code: CodeNoRoot, Severity: SeverityError,
			Message: "no root span: every span references a parent (pure cycle)",
		})
	}
	if cyclePath != nil {
		a.diags = append(a.diags, Diagnostic{
			Code: CodeCycle, Severity: SeverityError,
			Message: "parent cycle detected: " + joinCycle(cyclePath),
		})
	}

	a.checkIntervals()

	res := &Result{TraceID: trace.TraceID, RootSpanID: rootID}

	// Span timing rows (always available), with self time.
	selfTimes := map[string]int64{}
	for _, id := range a.order {
		n := a.nodes[id]
		selfTimes[id] = computeSelfTime(n)
	}
	for _, id := range a.order {
		n := a.nodes[id]
		res.SpanTimes = append(res.SpanTimes, SpanTime{
			SpanID:   id,
			Name:     n.span.Name,
			Relation: n.span.Relation,
			Duration: n.span.Duration(),
			SelfTime: selfTimes[id],
		})
	}

	// Critical path only exists for an acyclic trace with a root.
	hasError := cyclePath != nil || rootID == ""
	if !hasError {
		critical := map[string]int64{}
		var compute func(n *node) int64
		compute = func(n *node) int64 {
			if v, ok := critical[n.span.SpanID]; ok {
				return v
			}
			c := selfTimes[n.span.SpanID]
			var asyncMax int64
			haveAsync := false
			for _, ch := range n.children {
				v := compute(ch)
				if ch.span.Relation == model.RelationAsync {
					if !haveAsync || v > asyncMax {
						asyncMax = v
					}
					haveAsync = true
				} else {
					c += v
				}
			}
			if haveAsync {
				c += asyncMax
			}
			critical[n.span.SpanID] = c
			return c
		}
		for _, id := range a.order {
			compute(a.nodes[id])
		}
		for _, id := range a.order {
			n := a.nodes[id]
			v := critical[id]
			res.SpanTimes = setCritical(res.SpanTimes, id, v)
			if d := n.span.Duration(); d >= 0 && v > d {
				a.diags = append(a.diags, Diagnostic{
					Code: CodeCriticalExceedsDuration, Severity: SeverityWarning, SpanID: id,
					Message: "modeled critical time exceeds span wall-clock duration; " +
						"check sync/async labels and clock skew",
				})
			}
		}

		root := a.nodes[rootID]
		cp := critical[rootID]
		dur := root.span.Duration()
		res.TraceDuration = &dur
		res.CriticalPathDuration = &cp
		if dur > 0 {
			res.Ratio = round4(float64(cp) / float64(dur))
		}
		res.CriticalPath = a.buildPath(root, critical, selfTimes, "root")
	}

	a.sortDiagnostics()
	res.Diagnostics = a.diags
	if res.CriticalPath == nil {
		res.CriticalPath = []PathEntry{}
	}
	return res
}

// HasError reports whether diagnostics contain severity=error.
func (r *Result) HasError() bool {
	for _, d := range r.Diagnostics {
		if d.Severity == SeverityError {
			return true
		}
	}
	return false
}

func insertChildSorted(list []*node, n *node) []*node {
	i := sort.Search(len(list), func(i int) bool {
		x, y := list[i].span, n.span
		if x.StartTime != y.StartTime {
			return x.StartTime > y.StartTime
		}
		return x.SpanID > y.SpanID
	})
	list = append(list, nil)
	copy(list[i+1:], list[i:])
	list[i] = n
	return list
}

// findCycle returns the span ids of one cycle if the child graph has
// one, else nil.
func (a *analyzer) findCycle() []string {
	const white, gray, black = 0, 1, 2
	color := map[string]int{}
	var stack []string
	var found []string

	var dfs func(id string) bool
	dfs = func(id string) bool {
		color[id] = gray
		stack = append(stack, id)
		for _, ch := range a.nodes[id].children {
			cid := ch.span.SpanID
			switch color[cid] {
			case gray:
				for i, sid := range stack {
					if sid == cid {
						found = append([]string{}, stack[i:]...)
						found = append(found, cid)
						return true
					}
				}
			case white:
				if dfs(cid) {
					return true
				}
			}
		}
		stack = stack[:len(stack)-1]
		color[id] = black
		return false
	}

	for _, id := range a.order {
		if color[id] == white && dfs(id) {
			return found
		}
	}
	return nil
}

func joinCycle(ids []string) string {
	out := ""
	for i, id := range ids {
		if i > 0 {
			out += " -> "
		}
		out += id
	}
	return out
}

// checkIntervals emits SYNC_CHILD_OVERLAP and CHILD_OUTSIDE_PARENT
// diagnostics.
func (a *analyzer) checkIntervals() {
	for _, id := range a.order {
		n := a.nodes[id]
		var syncKids []*node
		for _, ch := range n.children {
			cs, ce := ch.span.StartTime, ch.span.EndTime
			if cs < n.span.StartTime || ce > n.span.EndTime {
				a.diags = append(a.diags, Diagnostic{
					Code: CodeChildOutsideParent, Severity: SeverityWarning, SpanID: ch.span.SpanID,
					Message: "child interval lies outside parent span's [start,end]",
				})
			}
			if ch.span.Relation == model.RelationSync {
				syncKids = append(syncKids, ch)
			}
		}
		// syncKids are already sorted by start time.
		for i := 1; i < len(syncKids); i++ {
			prev, cur := syncKids[i-1], syncKids[i]
			// Strict overlap: touching boundaries (prev.end == cur.start)
			// are serialized, not overlapping.
			if max64(prev.span.StartTime, cur.span.StartTime) <
				min64(prev.span.EndTime, cur.span.EndTime) {
				a.diags = append(a.diags, Diagnostic{
					Code: CodeSyncOverlap, Severity: SeverityWarning, SpanID: cur.span.SpanID,
					Message: "sync children " + prev.span.SpanID + " and " +
						cur.span.SpanID + " overlap in wall-clock time",
				})
			}
		}
	}
}

type interval struct{ start, end int64 }

// computeSelfTime implements the union-of-children rule.
func computeSelfTime(n *node) int64 {
	var ivs []interval
	dur := n.span.Duration()
	for _, ch := range n.children {
		st, en := ch.span.StartTime, ch.span.EndTime
		// Clip to the parent envelope; ignore empty / inverted pieces.
		if st < n.span.StartTime {
			st = n.span.StartTime
		}
		if en > n.span.EndTime {
			en = n.span.EndTime
		}
		if en > st {
			ivs = append(ivs, interval{st, en})
		}
	}
	sort.Slice(ivs, func(i, j int) bool {
		if ivs[i].start != ivs[j].start {
			return ivs[i].start < ivs[j].start
		}
		return ivs[i].end < ivs[j].end
	})
	var covered int64
	for i := 0; i < len(ivs); {
		st, en := ivs[i].start, ivs[i].end
		j := i + 1
		for j < len(ivs) && ivs[j].start <= en {
			if ivs[j].end > en {
				en = ivs[j].end
			}
			j++
		}
		covered += en - st
		i = j
	}
	return dur - covered
}

// buildPath emits the ordered critical path: the span itself, then its
// sync children and the longest async child(ren) interleaved by
// wall-clock start time. Each winning child is followed by its own
// path (DFS), so sync chains and parallel branches stay readable as
// subtrees. Losing async children are omitted.
func (a *analyzer) buildPath(n *node, critical, self map[string]int64, reason string) []PathEntry {
	id := n.span.SpanID
	out := []PathEntry{{
		SpanID:       id,
		Name:         n.span.Name,
		Relation:     n.span.Relation,
		SelfTime:     self[id],
		CriticalTime: critical[id],
		Reason:       reason,
	}}
	var asyncMax int64
	haveAsync := false
	for _, ch := range n.children {
		if ch.span.Relation == model.RelationAsync {
			v := critical[ch.span.SpanID]
			if !haveAsync || v > asyncMax {
				asyncMax = v
			}
			haveAsync = true
		}
	}
	// n.children is already sorted by (start, span id): walk it in
	// order, which interleaves sync branches with the async winner.
	for _, ch := range n.children {
		switch {
		case ch.span.Relation == model.RelationSync:
			out = append(out, a.buildPath(ch, critical, self, "sync-blocking")...)
		case haveAsync && critical[ch.span.SpanID] == asyncMax:
			out = append(out, a.buildPath(ch, critical, self, "async-parallel-longest")...)
		}
	}
	return out
}

func (a *analyzer) sortDiagnostics() {
	sort.SliceStable(a.diags, func(i, j int) bool {
		x, y := a.diags[i], a.diags[j]
		if x.Code != y.Code {
			return x.Code < y.Code
		}
		if x.SpanID != y.SpanID {
			return x.SpanID < y.SpanID
		}
		return x.Message < y.Message
	})
}

func setCritical(rows []SpanTime, id string, v int64) []SpanTime {
	for i := range rows {
		if rows[i].SpanID == id {
			rows[i].CriticalTime = &v
			return rows
		}
	}
	return rows
}

func round4(v float64) float64 {
	return math.Round(v*10000) / 10000
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
