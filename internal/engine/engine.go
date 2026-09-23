// Package engine 实现渐进发布的纯判定逻辑，不依赖数据库或 HTTP，便于测试。
package engine

import "fmt"

// 阶段流量权重：5% → 20% → 50% → 100%
var StageWeights = []int{5, 20, 50, 100}

const (
	VerdictPromote   = "promote"   // 满足全部条件，可推进
	VerdictHold      = "hold"      // 条件不足，继续观察
	VerdictViolation = "violation" // 指标完整但突破阈值
	VerdictUnknown   = "unknown"   // 指标缺失/不完整，判未知，绝不判健康
)

const (
	ReasonMetricsMissing      = "metrics_missing"
	ReasonMetricsPartial      = "metrics_partial"
	ReasonWindowIncomplete    = "observation_window_incomplete"
	ReasonInsufficientSamples = "insufficient_samples"
	ReasonErrorRateExceeded   = "error_rate_exceeded"
	ReasonLatencyExceeded     = "latency_exceeded"
	ReasonHealthy             = "within_thresholds"
)

// Thresholds 是冻结在发布开始时的阈值版本。
type Thresholds struct {
	MaxErrorRate    float64 `json:"max_error_rate"`
	MaxP95LatencyMs float64 `json:"max_p95_latency_ms"`
}

// Bucket 是桩服务返回的单时间桶指标，作为区间证据。
type Bucket struct {
	Start        string  `json:"start"`
	End          string  `json:"end"`
	Samples      int64   `json:"samples"`
	Errors       int64   `json:"errors"`
	P95LatencyMs float64 `json:"p95_latency_ms"`
	IngestedAt   string  `json:"ingested_at,omitempty"` // 数据到达时间（迟到数据可见）
}

// MetricsWindow 是桩服务对某个观察区间的汇总响应。
type MetricsWindow struct {
	BucketSeconds   int64    `json:"bucket_seconds"`
	ExpectedBuckets int      `json:"expected_buckets"` // [from,to) 理论桶数
	ReceivedBuckets int      `json:"received_buckets"` // 当前已到达桶数
	Buckets         []Bucket `json:"buckets"`
	Samples         int64    `json:"samples"`
	Errors          int64    `json:"errors"`
	P95LatencyMs    float64  `json:"p95_latency_ms"` // 对已到达桶的样本量加权 p95
}

// Coverage 返回区间覆盖率（0..1）。空区间视为 0。
func (m *MetricsWindow) Coverage() float64 {
	if m.ExpectedBuckets == 0 {
		return 0
	}
	return float64(m.ReceivedBuckets) / float64(m.ExpectedBuckets)
}

// Input 是一次判定的全部输入。
type Input struct {
	StageIdx       int
	MinSamples     int64
	WindowComplete bool // now >= stage_started_at + observation_window
	Window         *MetricsWindow
	Threshold      Thresholds
}

// Decision 是判定结果（含指标区间证据）。
type Decision struct {
	Verdict       string     `json:"verdict"`
	Reason        string     `json:"reason"`
	StageIdx      int        `json:"stage_idx"`
	StageWeight   int        `json:"stage_weight"`
	CanPromote    bool       `json:"can_promote"`
	Samples       int64      `json:"samples"`
	ErrorRate     *float64   `json:"error_rate"`     // 未知时为 nil
	P95LatencyMs  *float64   `json:"p95_latency_ms"` // 未知时为 nil
	Coverage      float64    `json:"coverage"`
	MetricsStatus string     `json:"metrics_status"` // ok | partial | missing
	Buckets       []Bucket   `json:"buckets"`
	Threshold     Thresholds `json:"threshold"`
}

// Evaluate 执行判定。判定顺序很重要：
//  1. 指标完全缺失 → unknown（不得视为健康）
//  2. 指标未覆盖整个观察区间 → unknown
//  3. 观察窗口未走完 → hold
//  4. 样本量不足 → hold
//  5. 指标完整且突破阈值 → violation（持续退化）
//  6. 否则 → promote（区间内短暂尖峰若未抬高聚合值，不阻断）
func Evaluate(in Input) Decision {
	d := Decision{
		StageIdx:    in.StageIdx,
		StageWeight: StageWeights[in.StageIdx],
		Buckets:     []Bucket{},
		Threshold:   in.Threshold,
	}
	if in.Window != nil && in.Window.Buckets != nil {
		d.Buckets = in.Window.Buckets
	}

	w := in.Window
	switch {
	case w == nil || w.ExpectedBuckets == 0 || w.ReceivedBuckets == 0:
		d.Coverage = 0
		d.MetricsStatus = "missing"
		d.Verdict, d.Reason = VerdictUnknown, ReasonMetricsMissing
		return d
	case w.ReceivedBuckets < w.ExpectedBuckets:
		d.Coverage = w.Coverage()
		d.MetricsStatus = "partial"
		d.Verdict, d.Reason = VerdictUnknown, ReasonMetricsPartial
		d.Samples = w.Samples
		return d
	}

	// 指标完整：覆盖率 100%
	d.Coverage = 1
	d.MetricsStatus = "ok"
	d.Samples = w.Samples
	errRate := 0.0
	if w.Samples > 0 {
		errRate = float64(w.Errors) / float64(w.Samples)
	}
	d.ErrorRate = &errRate
	p95 := w.P95LatencyMs
	d.P95LatencyMs = &p95

	if !in.WindowComplete {
		d.Verdict, d.Reason = VerdictHold, ReasonWindowIncomplete
		return d
	}
	if w.Samples < in.MinSamples {
		d.Verdict, d.Reason = VerdictHold, ReasonInsufficientSamples
		return d
	}
	if errRate > in.Threshold.MaxErrorRate {
		d.Verdict, d.Reason = VerdictViolation, ReasonErrorRateExceeded
		return d
	}
	if p95 > in.Threshold.MaxP95LatencyMs {
		d.Verdict, d.Reason = VerdictViolation, ReasonLatencyExceeded
		return d
	}
	d.Verdict, d.Reason = VerdictPromote, ReasonHealthy
	d.CanPromote = true
	return d
}

// NextStage 返回推进后的阶段索引；已在 100% 则返回完成标记。
func NextStage(idx int) (int, bool, error) {
	if idx < 0 || idx >= len(StageWeights) {
		return 0, false, fmt.Errorf("invalid stage index %d", idx)
	}
	if idx == len(StageWeights)-1 {
		return idx, true, nil // 100% 阶段再推进 → completed
	}
	return idx + 1, false, nil
}
