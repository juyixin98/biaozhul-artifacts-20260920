// Package analyzer 实现基于 span DAG 的关键路径分析。
//
// 核心概念：
//   - 一个 trace 由若干 span 组成，span 之间通过 ParentID 构成森林（通常单根）。
//   - ParentID 为空的 span 是根；同一父下的多个孩子默认表示“同步串行依赖”，
//     标记 Async=true 的孩子表示“并行子任务”（父不等待它完成，除非它超出父的结束时间）。
//   - 关键路径 = 在墙上时间（wall-clock）轴上真正决定整条 trace 耗时的区间链。
//     并行孩子即使自身耗时长，只要在父结束前完成（尾部收紧），就不在关键路径上。
//   - 自耗时（self time）用孩子区间的并集（union）从父区间中扣除，重叠只扣一次。
//
// 因此关键路径耗时绝不等于所有子 span 耗时之和：并行重叠部分只在墙上计一次。
package analyzer

// Span 是摄入的原始观测数据。时间字段一律用 int64，单位由调用方约定（样例用纳秒）。
type Span struct {
	TraceID  string `json:"trace_id"`
	SpanID   string `json:"span_id"`
	ParentID string `json:"parent_id,omitempty"`
	Name     string `json:"name"`
	StartUs  int64  `json:"start_us"` // 相对或绝对时间戳，只需满足同一 trace 内可比
	EndUs    int64  `json:"end_us"`
	// Async=true 表示该 span 是父下的并行子任务（fire-and-forget 或后台任务），
	// 默认 false 表示同步依赖：父的这段时间在等它。
	Async bool `json:"async,omitempty"`
}

// Severity 诊断级别。
type Severity string

const (
	SevError   Severity = "error"   // 输入结构性错误：循环、负时长等，结果不可信
	SevWarning Severity = "warning" // 时钟矛盾、孤儿等：仍给出结果，但区间按规则裁剪/归属
)

// Diagnostic 描述一条输入或时钟诊断。
type Diagnostic struct {
	Code    Severity `json:"severity"`
	Kind    string   `json:"kind"`
	SpanID  string   `json:"span_id,omitempty"`
	Message string   `json:"message"`
	Detail  string   `json:"detail,omitempty"`
}

// PathSpan 是关键路径上的一个归属区间。
type PathSpan struct {
	SpanID string `json:"span_id"`
	Name   string `json:"name"`
	// AttributedUs 该 span 在墙上关键路径上占有的时间（被更深层关键 span 覆盖的部分已扣掉）。
	AttributedUs int64 `json:"attributed_us"`
	// Async 该 span 是否为并行子任务（true 表示它以“尾部收紧”方式意外落在关键路径上）。
	Async bool `json:"async"`
}

// Report 是一次关键路径分析的完整结果。
type Report struct {
	TraceID string `json:"trace_id"`
	RootID  string `json:"root_id,omitempty"`
	// RootDurationUs 根 span 的墙上总时长 End-Start；多根时取主根。
	RootDurationUs int64 `json:"root_duration_us"`
	// CriticalPathDurationUs 关键路径归属时间之和。时钟一致且单根时等于 RootDurationUs。
	CriticalPathDurationUs int64 `json:"critical_path_duration_us"`
	// SumSpanDurationUs 所有 span 原始耗时之和，仅用于对照：它会把并行重复计数。
	SumSpanDurationUs int64 `json:"sum_span_duration_us"`
	// ParallelSlackUs 并行子任务在父结束前完成而省下的墙上时间总和（尾部收紧量）。
	ParallelSlackUs int64            `json:"parallel_slack_us"`
	CriticalPath    []PathSpan       `json:"critical_path"`
	SelfTimes       map[string]int64 `json:"self_times"`
	Diagnostics     []Diagnostic     `json:"diagnostics"`
	// HasError 存在 error 级诊断时为 true，HTTP 层据此返回 422。
	HasError bool `json:"has_error"`
}
