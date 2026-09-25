// Package trace 实现分布式追踪 span 的乱序拼装核心逻辑。
//
// 设计约定（详见 README“设计决策”）：
//   - 时间一律是摄入端指定的“合成逻辑时间”(receive_ns / watermark_ns)，
//     组装器内部绝不读取壁钟(now)，所有行为可确定复现；
//   - 因果关系只依据 parent_span_id 构成的父子图判定，绝不用时间戳断言先后；
//   - 修订(revision)是不可变快照，编号单调递增，用于核对“每版包含关系”。
package trace

import (
	"errors"
	"time"
)

// Span 是一个被摄入的 span。所有字段按 JSON tag 对外暴露。
type Span struct {
	TraceID      string `json:"trace_id"`
	SpanID       string `json:"span_id"`
	ParentSpanID string `json:"parent_span_id,omitempty"`
	Service      string `json:"service,omitempty"`
	Operation    string `json:"operation,omitempty"`
	// Start 是该服务本地时钟观察到的开始时刻；DurationNanos 为耗时。
	// 二者仅用于检测跨服务时钟偏差，绝不参与因果/结构判定。
	Start         time.Time         `json:"start"`
	DurationNanos int64             `json:"duration_nanos"`
	Attributes    map[string]string `json:"attributes,omitempty"`

	// ReceiveNS 是摄入时由调用方指定的合成逻辑接收时间（纳秒刻度，任意整数域）。
	ReceiveNS int64 `json:"receive_ns"`

	// seq 是组装器在一次进程内分配的全局单调到达序号，仅用于确定性排序。
	seq int64
}

// Conflict 记录同一 span_id 的重复/冲突摄入。
type Conflict struct {
	SpanID    string   `json:"span_id"`
	FirstSeq  int64    `json:"first_seq"`
	DupSeq    int64    `json:"dup_seq"`
	Kind      string   `json:"kind"` // "exact-duplicate" | "payload-conflict"
	Fields    []string `json:"fields,omitempty"`
	Duplicate Span     `json:"duplicate"`
}

// SkewEvent 记录一次父子本地时钟不一致（信息性告警，不影响结构）。
type SkewEvent struct {
	ParentSpanID string `json:"parent_span_id"`
	ChildSpanID  string `json:"child_span_id"`
	ParentStart  int64  `json:"parent_start_unix_nanos"`
	ChildStart   int64  `json:"child_start_unix_nanos"`
	ChildEnd     int64  `json:"child_end_unix_nanos"`
	ParentEnd    int64  `json:"parent_end_unix_nanos"`
	// delta_ns = child_start - parent_start，为负表示子的本地时钟早于父。
	DeltaNS int64  `json:"delta_ns"`
	Kind    string `json:"kind"` // "starts-before-parent" | "ends-after-parent"
}

// MissingParent 记录引用了尚未到达的父 span 的边。
type MissingParent struct {
	ChildID  string `json:"child_span_id"`
	ParentID string `json:"missing_parent_span_id"`
}

// Revision 是一次 trace 拼装结果的不可变快照。
type Revision struct {
	TraceID   string   `json:"trace_id"`
	Revision  int64    `json:"revision"`
	Closed    bool     `json:"closed"`
	Complete  bool     `json:"complete"`
	Reasons   []string `json:"reasons"`
	RevisedOf int64    `json:"revised_of,omitempty"`

	SpanIDs        []string        `json:"span_ids"`
	RootSpanIDs    []string        `json:"root_span_ids"`
	Spans          []Span          `json:"spans"`
	Conflicts      []Conflict      `json:"conflicts"`
	MissingParents []MissingParent `json:"missing_parents"`
	Cycles         [][]string      `json:"cycles"`
	Skew           []SkewEvent     `json:"clock_skew"`

	WatermarkNS int64 `json:"watermark_ns"`
	EmittedAtNS int64 `json:"emitted_at_ns"`
}

// 闭环原因常量。
const (
	ReasonTimeout   = "timeout"               // 窗口超时仍不完整
	ReasonComplete  = "complete"              // 图完整（根可达且无缺失边）
	ReasonRevised   = "late-arrival-revision" // 关闭后迟到数据补全/修正
	ReasonMultiRoot = "multiple-roots"        // 信息性：出现多个根
	ReasonCycle     = "cycle-detected"        // 父链存在循环
)

// ErrValidation 在摄入报文本身非法时返回；整条批次拒绝、状态不变。
var ErrValidation = errors.New("validation error")

func spanStartNS(s *Span) int64 { return s.Start.UnixNano() }
