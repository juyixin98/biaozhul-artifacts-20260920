package trace

import (
	"fmt"
	"sort"
	"sync"
)

// Sink 是修订/摄入事件的持久化出口（由 store 层实现；nil 表示纯内存）。
type Sink interface {
	// AppendSpan 在状态应用之前持久化一个摄入 span。
	AppendSpan(s Span) error
	// AppendWatermark 持久化显式水位推进。
	AppendWatermark(ns int64) error
	// AppendSweep 持久化“批次结束处按当前水位做一次超时扫描”的动作，
	// 保证重放时在相同的批边界产生完全相同的关闭修订。
	AppendSweep() error
	// SaveRevision 在修订产生后原子落盘快照。
	SaveRevision(rev Revision) error
}

// Config 控制组装器行为。时间单位均为 receive_ns 使用的同一逻辑刻度。
type Config struct {
	// TimeoutNS：某 trace 最后一次收到新规范 span 后，水位超过该时长仍无新数据即超时关闭。
	TimeoutNS int64
	// SkewToleranceNS：父子本地时钟偏差容忍带，带内不告警。
	SkewToleranceNS int64
}

// Assembler 是线程安全的 trace 拼装器。
type Assembler struct {
	cfg  Config
	mu   sync.Mutex
	sink Sink

	seq       int64
	watermark int64
	traces    map[string]*traceState
}

type traceState struct {
	// canonical 按“先到先得”保留规范 span（首份到达者）。
	canonical map[string]*Span
	order     []string // canonical span_id 的到达顺序
	conflicts []Conflict
	open      bool
	revision  int64
	lastRev   *Revision
	history   []Revision // 所有已关闭修订（编号=下标+1），用于逐版包含核对
	lastTouch int64      // 最近一次“被接受的新规范 span”的 receive_ns
}

// New 构造组装器。
func New(cfg Config, sink Sink) *Assembler {
	if cfg.SkewToleranceNS < 0 {
		cfg.SkewToleranceNS = 0
	}
	if cfg.TimeoutNS < 0 {
		cfg.TimeoutNS = 0
	}
	return &Assembler{
		cfg:       cfg,
		sink:      sink,
		watermark: -1,
		traces:    map[string]*traceState{},
	}
}

// Watermark 返回当前逻辑水位。
func (a *Assembler) Watermark() int64 {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.watermark
}

// AttachSink 挂载（或替换）持久化出口。仅供重放流程在状态重建完成后使用。
func (a *Assembler) AttachSink(s Sink) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.sink = s
}

// IngestResult 是单个 span 被并入后的判定结果。
type IngestResult struct {
	TraceID   string    `json:"trace_id"`
	SpanID    string    `json:"span_id"`
	Accepted  bool      `json:"accepted"`
	Duplicate bool      `json:"duplicate"`
	Conflict  *Conflict `json:"conflict,omitempty"`
}

// IngestBatch 摄入一批 span，并可隐式把水位推进到 atLeastWatermarkNS（传 -1 忽略）。
// 返回逐 span 的摄入判定，以及本批次引起的关闭修订（按 trace、编号升序）。
// 任一 span 非法则整批拒绝（ErrValidation），状态完全不变。
func (a *Assembler) IngestBatch(spans []Span, atLeastWatermarkNS int64) ([]IngestResult, []Revision, error) {
	a.mu.Lock()
	defer a.mu.Unlock()

	for i := range spans {
		if err := validate(&spans[i]); err != nil {
			return nil, nil, fmt.Errorf("%w: span[%d] %v", ErrValidation, i, err)
		}
	}

	results := make([]IngestResult, 0, len(spans))
	var emitted []Revision
	for i := range spans {
		s := spans[i]
		if a.sink != nil {
			if err := a.sink.AppendSpan(s); err != nil {
				return results, emitted, err
			}
		}
		res := a.applyOne(&s)
		results = append(results, res)
	}
	// 隐式水位推进与显式推进同样持久化，保证重放逐字节复现修订。
	if atLeastWatermarkNS > a.watermark {
		if a.sink != nil {
			if err := a.sink.AppendWatermark(atLeastWatermarkNS); err != nil {
				return results, emitted, err
			}
		}
	}
	if atLeastWatermarkNS > a.watermark {
		a.watermark = atLeastWatermarkNS
	}
	// 批边界补扫：新到达的 trace 可能已经落后于当前全局水位（例如水位 500
	// 时才收到 receive_ns=20 的 span）。扫描点作为独立 WAL 事件持久化。
	if a.sink != nil && a.watermark >= 0 {
		if err := a.sink.AppendSweep(); err != nil {
			return results, emitted, err
		}
	}
	emitted = append(emitted, a.sweepLocked()...)
	return results, emitted, nil
}

// applyOne 分配序号、必要时重开已关闭 trace，并把 span 并入对应 trace。
func (a *Assembler) applyOne(s *Span) IngestResult {
	a.seq++
	s.seq = a.seq

	st := a.traces[s.TraceID]
	if st == nil {
		st = &traceState{canonical: map[string]*Span{}, open: true}
		a.traces[s.TraceID] = st
	}

	// 已关闭 trace 的迟到数据：
	//   - 全新 span_id：重开；
	//   - 已有 id 的负载冲突：重开（让冲突进入下一修订）；
	//   - 精确重复：什么都不改变，不产生修订。
	if !st.open {
		if existing, exists := st.canonical[s.SpanID]; !exists {
			st.open = true
		} else if _, _, isConflict := classifyPair(existing, s); isConflict {
			st.open = true
		}
	}

	if existing, ok := st.canonical[s.SpanID]; ok {
		kind, fields, isConflict := classifyPair(existing, s)
		if isConflict {
			c := Conflict{
				SpanID:    s.SpanID,
				FirstSeq:  existing.seq,
				DupSeq:    s.seq,
				Kind:      kind,
				Fields:    fields,
				Duplicate: *s,
			}
			st.conflicts = append(st.conflicts, c)
			// 冲突是新信息：刷新 idle 计时，使冲突 trace 与新 span 一样获得完整超时窗口。
			if s.ReceiveNS > st.lastTouch {
				st.lastTouch = s.ReceiveNS
			}
			return IngestResult{TraceID: s.TraceID, SpanID: s.SpanID, Accepted: false, Duplicate: true, Conflict: &c}
		}
		return IngestResult{TraceID: s.TraceID, SpanID: s.SpanID, Accepted: false, Duplicate: true}
	}

	cp := *s
	st.canonical[s.SpanID] = &cp
	st.order = append(st.order, s.SpanID)
	if s.ReceiveNS > st.lastTouch {
		st.lastTouch = s.ReceiveNS
	}
	return IngestResult{TraceID: s.TraceID, SpanID: s.SpanID, Accepted: true}
}

// classifyPair 比较同一 span_id 的两份负载，返回 kind、冲突字段列表、是否冲突。
func classifyPair(first, s *Span) (string, []string, bool) {
	var fields []string
	if s.ParentSpanID != first.ParentSpanID {
		fields = append(fields, "parent_span_id")
	}
	if s.Service != first.Service {
		fields = append(fields, "service")
	}
	if s.Operation != first.Operation {
		fields = append(fields, "operation")
	}
	if !s.Start.Equal(first.Start) {
		fields = append(fields, "start")
	}
	if s.DurationNanos != first.DurationNanos {
		fields = append(fields, "duration_nanos")
	}
	if attrsDiffer(s.Attributes, first.Attributes) {
		fields = append(fields, "attributes")
	}
	// receive_ns 是摄入元数据而非 span 负载：不同 receive_ns 不构成冲突。
	if len(fields) > 0 {
		sort.Strings(fields)
		return "payload-conflict", fields, true
	}
	return "exact-duplicate", nil, false
}

func attrsDiffer(x, y map[string]string) bool {
	if len(x) != len(y) {
		return true
	}
	for k, v := range x {
		if yv, ok := y[k]; !ok || yv != v {
			return true
		}
	}
	return false
}

func validate(s *Span) error {
	if s.TraceID == "" {
		return fmt.Errorf("trace_id is required")
	}
	if s.SpanID == "" {
		return fmt.Errorf("span_id is required")
	}
	if s.Start.IsZero() {
		return fmt.Errorf("start is required (RFC3339)")
	}
	if s.DurationNanos < 0 {
		return fmt.Errorf("duration_nanos must be >= 0")
	}
	return nil
}

// AdvanceWatermark 显式推进逻辑水位，可能触发超时关闭。水位单调：回退被忽略。
func (a *Assembler) AdvanceWatermark(ns int64) ([]Revision, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.sink != nil && ns > a.watermark {
		if err := a.sink.AppendWatermark(ns); err != nil {
			return nil, err
		}
	}
	return a.advanceWatermarkLocked(ns), nil
}

func (a *Assembler) advanceWatermarkLocked(ns int64) []Revision {
	if ns <= a.watermark {
		return nil
	}
	a.watermark = ns
	return a.sweepLocked()
}

// sweepLocked 关闭所有“空闲超时”的打开 trace。
func (a *Assembler) sweepLocked() []Revision {
	var ids []string
	for id, st := range a.traces {
		if st.open && a.watermark-st.lastTouch >= a.cfg.TimeoutNS {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids) // 确定性顺序
	var out []Revision
	for _, id := range ids {
		st := a.traces[id]
		st.open = false
		rev := a.buildRevision(st, id)
		if !rev.Complete {
			rev.Reasons = append(rev.Reasons, ReasonTimeout)
		}
		rev = a.emitLocked(st, rev)
		out = append(out, rev)
	}
	return out
}

// emitLocked 分配修订编号、持久化并更新 lastRev。
func (a *Assembler) emitLocked(st *traceState, rev Revision) Revision {
	st.revision++
	rev.Revision = st.revision
	if st.lastRev != nil && st.lastRev.Closed {
		rev.RevisedOf = st.lastRev.Revision
		rev.Reasons = append(rev.Reasons, ReasonRevised)
	}
	if a.sink != nil {
		if err := a.sink.SaveRevision(rev); err != nil {
			panic(persistError{err}) // 不制造“已应答但未落盘”的假象
		}
	}
	cloned := cloneRevision(rev)
	st.lastRev = &cloned
	st.history = append(st.history, cloned)
	return rev
}

type persistError struct{ err error }

func (e persistError) Error() string { return "persistence error: " + e.err.Error() }
