// Package assembler reconstructs traces from out-of-order spans.
//
// Design rules (see README "设计原则"):
//
//   - Causality is derived ONLY from span references (parentSpanId). Span
//     timestamps never determine ordering, ancestry, or completeness. This
//     makes assembly immune to cross-service clock skew.
//   - Every accepted span gets a monotonically increasing ingest sequence,
//     so duplicate/conflict/late decisions are deterministic and replayable
//     from the WAL.
//   - Each material change appends an immutable Revision. A revision's spanId
//     set is a superset of every preceding revision's set.
//   - Timeout is driven by an injectable Clock and an explicit Sweep; no
//     wall-clock sleeps occur in assembly logic.
package assembler

import (
	"fmt"
	"sort"
	"sync"
	"time"

	"tracestitch/internal/clock"
	"tracestitch/internal/model"
)

// Revision reasons.
const (
	ReasonInitial   = "initial"   // first span of a trace
	ReasonExtended  = "extended"  // a new span attached (child-before-parent etc.)
	ReasonCompleted = "completed" // a previously incomplete trace now has a full root chain
	ReasonTimeout   = "timeout"   // incomplete trace flushed after timeout
	ReasonLate      = "late"      // span arrived after the trace was sealed by timeout
	ReasonConflict  = "conflict"  // rejected duplicate with a conflicting payload
)

// Config controls assembly behaviour.
type Config struct {
	// Timeout is how long an incomplete trace may stay open before Sweep
	// seals it and emits an "timeout" incomplete revision.
	Timeout time.Duration
	// ClockSkewTolerance: child start earlier than parent start (or child end
	// later than parent end) by more than this is flagged. Zero means any
	// nesting violation is flagged.
	ClockSkewTolerance time.Duration
}

// DefaultConfig gives a 30s timeout and 1ms skew tolerance.
func DefaultConfig() Config {
	return Config{Timeout: 30 * time.Second, ClockSkewTolerance: time.Millisecond}
}

// Store is the persistence seam. All accepted spans are appended before
// state changes, so a crash can replay the whole trace deterministically.
type Store interface {
	Append(rec WALRecord) error
	SaveSnapshot(traceID string, view model.TraceView) error
}

// IngestResult reports what happened to one submitted span.
type IngestResult struct {
	TraceID         string   `json:"traceId"`
	SpanID          string   `json:"spanId"`
	Status          string   `json:"status"` // "accepted" | "duplicate" | "conflict"
	IngestSeq       int64    `json:"ingestSeq,omitempty"`
	Revision        int      `json:"revision,omitempty"` // new revision version, 0 if none
	Reason          string   `json:"reason,omitempty"`
	DifferingFields []string `json:"differingFields,omitempty"`
	Error           string   `json:"error,omitempty"`
}

// Assembler is the concurrency-safe trace store.
type Assembler struct {
	cfg   Config
	clk   clock.Clock
	store Store
	logf  func(format string, args ...any)

	mu     sync.Mutex
	seq    int64
	traces map[string]*traceState
}

type traceState struct {
	id        string
	byID      map[string]*acceptedSpan
	order     []string // span ids in first-seen order
	conflicts []model.Conflict
	revs      []model.Revision
	sealed    bool
	deadline  time.Time // zero once the trace is complete
}

type acceptedSpan struct {
	span      model.Span
	ingestSeq int64
	seenAt    time.Time
}

// New builds an Assembler. clk defaults to the system clock; store may be nil
// for an in-memory-only instance.
func New(cfg Config, clk clock.Clock, store Store, logf func(string, ...any)) *Assembler {
	if clk == nil {
		clk = clock.System{}
	}
	if logf == nil {
		logf = func(string, ...any) {}
	}
	return &Assembler{
		cfg:    cfg,
		clk:    clk,
		store:  store,
		logf:   logf,
		traces: make(map[string]*traceState),
	}
}

// Ingest accepts one span. It persists the span first (so it survives a crash)
// and then updates in-memory state.
func (a *Assembler) Ingest(s model.Span) IngestResult {
	if err := validate(s); err != nil {
		return IngestResult{TraceID: s.TraceID, SpanID: s.SpanID, Status: "invalid", Error: err.Error()}
	}

	a.mu.Lock()
	defer a.mu.Unlock()

	now := a.clk.Now()

	// Persist before touching state. The sequence number is assigned up front
	// so WAL order and in-memory order can never diverge.
	a.seq++
	seq := a.seq
	rec := WALRecord{
		Event:            EventSpan,
		TraceID:          s.TraceID,
		IngestSeq:        seq,
		ReceivedUnixNano: now.UnixNano(),
		Span:             &s,
	}
	if a.store != nil {
		if err := a.store.Append(rec); err != nil {
			a.seq-- // hand the sequence back; nothing was observed
			return IngestResult{TraceID: s.TraceID, SpanID: s.SpanID, Status: "error", Error: err.Error()}
		}
	}

	return a.applySpan(rec, now)
}

// Replay re-applies records read from the WAL after a restart. It never writes
// to the store (the events are already there) and rebuilds every revision,
// including sealed/timeout state.
func (a *Assembler) Replay(recs []WALRecord) {
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, rec := range recs {
		at := time.Unix(0, rec.ReceivedUnixNano)
		if rec.Event == EventTimeout {
			a.applyTimeout(rec, at)
			continue
		}
		if rec.Span != nil { // EventSpan, including records written before Event existed
			a.applySpan(rec, at)
		}
	}
}

// applySpan performs the state transition for a persisted span record. Used
// both by Ingest and by WAL replay (which passes the stored sequence/time).
func (a *Assembler) applySpan(rec WALRecord, now time.Time) IngestResult {
	s := *rec.Span
	st := a.traces[s.TraceID]
	if st == nil {
		st = &traceState{id: s.TraceID, byID: make(map[string]*acceptedSpan)}
		a.traces[s.TraceID] = st
	}

	if rec.IngestSeq > a.seq {
		a.seq = rec.IngestSeq
	}

	if existing, ok := st.byID[s.SpanID]; ok {
		if fields := diffSpan(existing.span, s); len(fields) == 0 {
			// Identical resend: idempotent, no revision.
			return IngestResult{
				TraceID: s.TraceID, SpanID: s.SpanID, Status: "duplicate",
				IngestSeq: existing.ingestSeq,
			}
		} else {
			// Same id, different payload: first-write-wins; record conflict.
			st.conflicts = append(st.conflicts, model.Conflict{
				SpanID:            s.SpanID,
				ReceivedIngestSeq: rec.IngestSeq,
				ExistingIngestSeq: existing.ingestSeq,
				DifferingFields:   fields,
			})
			v := a.appendRevisionLocked(st, ReasonConflict, rec.IngestSeq, now)
			a.persistSnapshot(st)
			return IngestResult{
				TraceID: s.TraceID, SpanID: s.SpanID, Status: "conflict",
				IngestSeq: existing.ingestSeq, Revision: v, Reason: ReasonConflict,
				DifferingFields: fields,
			}
		}
	}

	wasComplete := false
	wasSealed := st.sealed
	if len(st.revs) > 0 {
		wasComplete = st.revs[len(st.revs)-1].Complete
	}

	st.byID[s.SpanID] = &acceptedSpan{span: s, ingestSeq: rec.IngestSeq, seenAt: now}
	st.order = append(st.order, s.SpanID)

	reason := ReasonInitial
	switch {
	case len(st.revs) == 0:
		reason = ReasonInitial
	case wasSealed:
		reason = ReasonLate
	default:
		// Could complete now, but start from extended and refine below.
		reason = ReasonExtended
	}

	an := a.analyze(st)
	if len(st.revs) > 0 && !wasSealed && !wasComplete && an.complete {
		reason = ReasonCompleted
	}

	v := a.appendRevisionLocked(st, reason, rec.IngestSeq, now, an)

	// Deadline bookkeeping: incomplete traces age out; completed ones never do.
	if an.complete {
		// A late span that completes a previously sealed trace unseals it;
		// the timeout itself is preserved in the revision history.
		st.sealed = false
		st.deadline = time.Time{}
	} else {
		if st.sealed {
			// Late span on a sealed still-incomplete trace: fresh window.
			st.deadline = now.Add(a.cfg.Timeout)
		} else {
			st.deadline = now.Add(a.cfg.Timeout)
		}
	}

	a.persistSnapshot(st)
	return IngestResult{
		TraceID: s.TraceID, SpanID: s.SpanID, Status: "accepted",
		IngestSeq: rec.IngestSeq, Revision: v, Reason: reason,
	}
}

// Sweep seals every still-open incomplete trace whose deadline has passed and
// emits a "timeout" incomplete revision. Returns the number of traces sealed.
// The server calls this on a ticker; tests call it after advancing the clock.
func (a *Assembler) Sweep() int {
	a.mu.Lock()
	defer a.mu.Unlock()

	now := a.clk.Now()
	sealed := 0
	for _, st := range a.traces {
		if st.sealed || st.deadline.IsZero() || now.Before(st.deadline) {
			continue
		}
		if a.sealLocked(st.id, now) {
			sealed++
		}
	}
	return sealed
}

// FlushTrace force-sweeps one trace regardless of its deadline. It is used by
// the deterministic demo/end-to-end test instead of waiting real wall time.
// Returns false if the trace does not exist, is already sealed or is complete.
func (a *Assembler) FlushTrace(traceID string) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	st := a.traces[traceID]
	if st == nil || st.sealed || len(st.revs) == 0 {
		return false
	}
	if st.revs[len(st.revs)-1].Complete {
		return false // complete traces are never sealed
	}
	return a.sealLocked(traceID, a.clk.Now())
}

// sealLocked marks a trace sealed, persists the timeout event and appends the
// "timeout" revision. Caller must hold a.mu.
func (a *Assembler) sealLocked(traceID string, now time.Time) bool {
	st := a.traces[traceID]
	if st == nil || st.sealed {
		return false
	}
	a.seq++
	rec := WALRecord{
		Event:            EventTimeout,
		TraceID:          traceID,
		IngestSeq:        a.seq,
		ReceivedUnixNano: now.UnixNano(),
	}
	if a.store != nil {
		if err := a.store.Append(rec); err != nil {
			a.seq--
			a.logf("persist timeout for trace %s failed: %v", traceID, err)
			return false
		}
	}
	return a.applyTimeout(rec, now)
}

// applyTimeout applies a persisted/replayed timeout event without writing to
// the store. Caller must hold a.mu.
func (a *Assembler) applyTimeout(rec WALRecord, now time.Time) bool {
	st := a.traces[rec.TraceID]
	if st == nil || st.sealed {
		return false
	}
	if rec.IngestSeq > a.seq {
		a.seq = rec.IngestSeq
	}
	st.sealed = true
	st.deadline = time.Time{}
	a.appendRevisionLocked(st, ReasonTimeout, rec.IngestSeq, now)
	a.persistSnapshot(st)
	return true
}

// appendRevisionLocked analyzes the trace (unless one is supplied) and appends
// an immutable revision describing the current span set.
func (a *Assembler) appendRevisionLocked(st *traceState, reason string, seq int64, at time.Time, supplied ...*analysis) int {
	var an *analysis
	if len(supplied) > 0 {
		an = supplied[0]
	} else {
		an = a.analyze(st)
	}

	spanIDs := make([]string, 0, len(st.order))
	spanIDs = append(spanIDs, st.order...)
	sort.Strings(spanIDs)

	conflictIDs := make([]string, 0, len(st.conflicts))
	for _, c := range st.conflicts {
		conflictIDs = append(conflictIDs, c.SpanID)
	}

	rev := model.Revision{
		Version:         len(st.revs) + 1,
		Reason:          reason,
		AtUnixNano:      at.UnixNano(),
		IngestSeq:       seq,
		SpanIDs:         spanIDs,
		RootSpanID:      an.canonicalRoot,
		Complete:        an.complete,
		MissingRoot:     an.missingRoot,
		HasOrphans:      len(an.orphans) > 0,
		HasCycles:       len(an.cycles) > 0,
		CyclePath:       an.canonicalCyclePath,
		ClockSkew:       an.skew,
		ConflictSpanIDs: conflictIDs,
	}
	st.revs = append(st.revs, rev)
	return rev.Version
}

// ---- analysis ---------------------------------------------------------------

type analysis struct {
	complete           bool
	missingRoot        bool
	canonicalRoot      string
	roots              []string // parentSpanId == ""
	orphans            map[string]bool
	cycles             map[string]bool
	canonicalCyclePath []string
	skew               []model.ClockSkewWarning
	forest             []*model.SpanNode
}

// analyze inspects the current span set WITHOUT mutating it. All structural
// facts (roots, orphans, cycles, skew) are recomputed on every revision.
func (a *Assembler) analyze(st *traceState) *analysis {
	an := &analysis{orphans: make(map[string]bool), cycles: make(map[string]bool)}

	byID := make(map[string]model.Span, len(st.order))
	children := make(map[string][]string) // parent id -> child ids (in-trace only)
	for _, id := range st.order {
		sp := st.byID[id].span
		byID[id] = sp
		if sp.ParentSpanID == "" {
			an.roots = append(an.roots, id)
		} else if _, ok := st.byID[sp.ParentSpanID]; ok {
			children[sp.ParentSpanID] = append(children[sp.ParentSpanID], id)
		} else {
			an.orphans[id] = true
		}
	}

	// Cycle detection over the functional parent graph, restricted to edges
	// whose endpoints both exist in the trace.
	an.canonicalCyclePath, an.cycles = detectCycles(byID)

	an.missingRoot = len(an.roots) == 0

	// One canonical root is the healthy case. Multiple roots is an anomaly
	// (fragments); zero roots is missing-root.
	sort.Strings(an.roots)
	switch len(an.roots) {
	case 1:
		an.canonicalRoot = an.roots[0]
	case 0:
		// If every node is orphaned or in a cycle there is no candidate root.
	default:
		an.canonicalRoot = an.roots[0] // deterministic; multi-root still incomplete
	}

	an.complete = len(an.roots) == 1 && len(an.orphans) == 0 && len(an.cycles) == 0

	// Clock skew over intact parent->child edges, in deterministic order.
	parentIDs := make([]string, 0, len(children))
	for pid := range children {
		parentIDs = append(parentIDs, pid)
	}
	sort.Strings(parentIDs)
	tol := a.cfg.ClockSkewTolerance.Nanoseconds()
	for _, pid := range parentIDs {
		parent := byID[pid]
		kids := append([]string(nil), children[pid]...)
		sort.Strings(kids)
		for _, cid := range kids {
			if an.cycles[cid] || an.cycles[pid] {
				continue // cycles are a separate anomaly; skip skew on them
			}
			child := byID[cid]
			if w, ok := skewOnEdge(parent, child, tol); ok {
				an.skew = append(an.skew, w)
			}
		}
	}

	an.forest = a.buildForest(byID, children, an)
	return an
}

func skewOnEdge(parent, child model.Span, tol int64) (model.ClockSkewWarning, bool) {
	switch {
	case child.StartUnixNano < parent.StartUnixNano-tol:
		return model.ClockSkewWarning{
			ParentID: parent.SpanID, ChildID: child.SpanID,
			ParentService: parent.ServiceName, ChildService: child.ServiceName,
			ChildStartNanos: child.StartUnixNano, ParentStartNanos: parent.StartUnixNano,
			StartDeltaNanos: child.StartUnixNano - parent.StartUnixNano,
			ToleranceNanos:  tol, Kind: "child-starts-before-parent",
		}, true
	case child.EndUnixNano > parent.EndUnixNano+tol:
		return model.ClockSkewWarning{
			ParentID: parent.SpanID, ChildID: child.SpanID,
			ParentService: parent.ServiceName, ChildService: child.ServiceName,
			ChildStartNanos: child.StartUnixNano, ParentStartNanos: parent.StartUnixNano,
			StartDeltaNanos: child.StartUnixNano - parent.StartUnixNano,
			ToleranceNanos:  tol, Kind: "child-ends-after-parent",
		}, true
	default:
		return model.ClockSkewWarning{}, false
	}
}

// detectCycles returns the canonical cycle path (closed loop) and the set of
// every span whose parent chain contains a cycle.
//
// The parent relation is functional (each node has <=1 parent), so each node
// belongs to at most one cycle; trees feeding into a cycle are marked too.
// The canonical path starts at the lexicographically smallest span id on the
// loop for stable output.
func detectCycles(byID map[string]model.Span) ([]string, map[string]bool) {
	const (
		unvisited = 0
		onStack   = 1
		done      = 2
	)
	color := make(map[string]int, len(byID))
	onStackAt := make(map[string]int) // id -> position in stack
	var stack []string
	inCycle := make(map[string]bool)
	var canonical []string

	visit := func(start string) {
		stack = stack[:0]
		cur := start
		for {
			if color[cur] == done {
				// Feeder chain leading into an already-known cycle is itself
				// trapped on the cycle (f -> a where a is on a loop).
				if inCycle[cur] {
					for _, n := range stack {
						inCycle[n] = true
					}
				}
				for _, n := range stack {
					color[n] = done
				}
				return
			}
			if color[cur] == onStack {
				// Found a loop: stack[idx:] are cycle members.
				idx := onStackAt[cur]
				loop := append([]string(nil), stack[idx:]...)
				for _, n := range loop {
					inCycle[n] = true
				}
				entry := loop[0]
				for _, n := range loop[1:] {
					if n < entry {
						entry = n
					}
				}
				// Rotate loop to begin at entry, then close it by repeating entry.
				var path []string
				for i := 0; i < len(loop); i++ {
					path = append(path, loop[(idxOf(loop, entry)+i)%len(loop)])
				}
				path = append(path, entry)
				if canonical == nil || lessPath(path, canonical) {
					canonical = path
				}
				// Feeder nodes before idx lead into the cycle; mark them too.
				for _, n := range stack[:idx] {
					inCycle[n] = true
				}
				for _, n := range stack {
					color[n] = done
				}
				return
			}
			color[cur] = onStack
			onStackAt[cur] = len(stack)
			stack = append(stack, cur)

			sp, ok := byID[cur]
			if !ok { // parent missing -> orphan edge, not a cycle
				for _, n := range stack {
					color[n] = done
				}
				return
			}
			next := sp.ParentSpanID
			if next == "" {
				for _, n := range stack {
					color[n] = done
				}
				return
			}
			if _, exists := byID[next]; !exists {
				for _, n := range stack {
					color[n] = done
				}
				return
			}
			cur = next
		}
	}

	ids := make([]string, 0, len(byID))
	for id := range byID {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		if color[id] == unvisited {
			visit(id)
		}
	}
	return canonical, inCycle
}

func idxOf(xs []string, v string) int {
	for i, x := range xs {
		if x == v {
			return i
		}
	}
	return -1
}

func lessPath(a, b []string) bool {
	for i := 0; i < len(a) && i < len(b); i++ {
		if a[i] != b[i] {
			return a[i] < b[i]
		}
	}
	return len(a) < len(b)
}

// buildForest renders the assembled tree. Canonical roots first; orphan roots
// and cycle-attached fragments follow so every span appears exactly once.
func (a *Assembler) buildForest(byID map[string]model.Span, children map[string][]string, an *analysis) []*model.SpanNode {
	placed := make(map[string]bool)
	var forest []*model.SpanNode

	var build func(id string, depth int, path []string) *model.SpanNode
	build = func(id string, depth int, path []string) *model.SpanNode {
		if placed[id] {
			return nil
		}
		placed[id] = true
		sp := byID[id]
		node := &model.SpanNode{
			Span:    sp,
			Depth:   depth,
			Path:    append(append([]string(nil), path...), id),
			Orphan:  an.orphans[id],
			InCycle: an.cycles[id],
		}
		if node.InCycle {
			node.CycleEntryID = an.canonicalCyclePath[0]
		}
		kids := append([]string(nil), children[id]...)
		sort.Strings(kids)
		for _, cid := range kids {
			if c := build(cid, depth+1, node.Path); c != nil {
				node.Children = append(node.Children, c)
			}
		}
		return node
	}

	// Deterministic canonical roots.
	for _, rid := range an.roots {
		if n := build(rid, 0, nil); n != nil {
			forest = append(forest, n)
		}
	}

	// Fragment roots: orphan subtree roots (parent absent from the trace) and
	// the canonical cycle entry, which reaches every cycle member and every
	// feeder node through the children map.
	var fragRoots []string
	for id := range byID {
		if placed[id] {
			continue
		}
		if an.orphans[id] {
			fragRoots = append(fragRoots, id)
		}
	}
	if an.canonicalCyclePath != nil {
		if entry := an.canonicalCyclePath[0]; !placed[entry] {
			fragRoots = append(fragRoots, entry)
		}
	}
	sort.Strings(fragRoots)
	for _, id := range fragRoots {
		if n := build(id, 0, nil); n != nil {
			forest = append(forest, n)
		}
	}
	// Defensive: place any node still unplaced on its own.
	for id := range byID {
		if !placed[id] {
			if n := build(id, 0, nil); n != nil {
				forest = append(forest, n)
			}
		}
	}
	return forest
}

// ---- queries ----------------------------------------------------------------

// GetTrace returns the assembled view of one trace, or ok=false.
func (a *Assembler) GetTrace(traceID string) (model.TraceView, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	st := a.traces[traceID]
	if st == nil {
		return model.TraceView{}, false
	}
	return a.viewLocked(st), true
}

// GetRevision returns one revision (1-based) of a trace.
func (a *Assembler) GetRevision(traceID string, version int) (model.Revision, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	st := a.traces[traceID]
	if st == nil || version < 1 || version > len(st.revs) {
		return model.Revision{}, false
	}
	return st.revs[version-1], true
}

// ListTraces returns trace ids in lexicographic order.
func (a *Assembler) ListTraces() []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	ids := make([]string, 0, len(a.traces))
	for id := range a.traces {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

func (a *Assembler) viewLocked(st *traceState) model.TraceView {
	an := a.analyze(st)
	spans := make([]model.Span, 0, len(st.order))
	for _, id := range st.order {
		spans = append(spans, st.byID[id].span)
	}
	revs := append([]model.Revision(nil), st.revs...)
	conflicts := append([]model.Conflict(nil), st.conflicts...)
	v := model.TraceView{
		TraceID:   st.id,
		Sealed:    st.sealed,
		Complete:  an.complete,
		Spans:     spans,
		Forest:    an.forest,
		Conflicts: conflicts,
		Revisions: revs,
	}
	if len(revs) > 0 {
		latest := revs[len(revs)-1]
		v.Latest = &latest
	}
	return v
}

// ---- revision containment ----------------------------------------------------

// ContainmentViolation describes a broken inclusion invariant between two
// consecutive revisions.
type ContainmentViolation struct {
	FromVersion int      `json:"fromVersion"`
	ToVersion   int      `json:"toVersion"`
	MissingIDs  []string `json:"missingIds"` // ids present in From but absent in To
}

// CheckRevisionContainment verifies that every revision's spanId set contains
// the previous revision's set. Returns nil when the invariant holds.
func (a *Assembler) CheckRevisionContainment(traceID string) *ContainmentViolation {
	a.mu.Lock()
	defer a.mu.Unlock()
	st := a.traces[traceID]
	if st == nil {
		return nil
	}
	var prev map[string]bool
	for i := range st.revs {
		cur := make(map[string]bool, len(st.revs[i].SpanIDs))
		for _, id := range st.revs[i].SpanIDs {
			cur[id] = true
		}
		if prev != nil {
			var missing []string
			for id := range prev {
				if !cur[id] {
					missing = append(missing, id)
				}
			}
			if len(missing) > 0 {
				sort.Strings(missing)
				return &ContainmentViolation{
					FromVersion: i, ToVersion: i + 1, MissingIDs: missing,
				}
			}
		}
		prev = cur
	}
	return nil
}

// ---- helpers -----------------------------------------------------------------

func validate(s model.Span) error {
	if s.TraceID == "" {
		return fmt.Errorf("traceId is required")
	}
	if s.SpanID == "" {
		return fmt.Errorf("spanId is required")
	}
	if s.EndUnixNano != 0 && s.StartUnixNano != 0 && s.EndUnixNano < s.StartUnixNano {
		return fmt.Errorf("span %s: endUnixNano %d is before startUnixNano %d", s.SpanID, s.EndUnixNano, s.StartUnixNano)
	}
	return nil
}

// diffSpan lists payload fields that differ between a stored span and a
// resubmitted span with the same id. Identity fields (trace/span id) are
// compared too, since a duplicate id under another trace is impossible here
// but parent/name/service/content drift must be caught.
func diffSpan(a, b model.Span) []string {
	var fields []string
	if a.ParentSpanID != b.ParentSpanID {
		fields = append(fields, "parentSpanId")
	}
	if a.ServiceName != b.ServiceName {
		fields = append(fields, "serviceName")
	}
	if a.Name != b.Name {
		fields = append(fields, "name")
	}
	if a.StartUnixNano != b.StartUnixNano {
		fields = append(fields, "startUnixNano")
	}
	if a.EndUnixNano != b.EndUnixNano {
		fields = append(fields, "endUnixNano")
	}
	if !sameAttributes(a.Attributes, b.Attributes) {
		fields = append(fields, "attributes")
	}
	return fields
}

func sameAttributes(x, y map[string]string) bool {
	if len(x) != len(y) {
		return false
	}
	for k, v := range x {
		if yv, ok := y[k]; !ok || yv != v {
			return false
		}
	}
	return true
}

func (a *Assembler) persistSnapshot(st *traceState) {
	if a.store == nil {
		return
	}
	if err := a.store.SaveSnapshot(st.id, a.viewLocked(st)); err != nil {
		a.logf("snapshot for trace %s failed: %v", st.id, err)
	}
}
