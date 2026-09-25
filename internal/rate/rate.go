// Package rate 实现单调计数器的区间增长与速率计算。
//
// 核心约定（务必先读，README 有中文版详述）：
//
//  1. 对相邻观测 (t1,v1)→(t2,v2)：
//     - v2 >= v1：无重置，观测区间增量下界 = v2-v1。
//     - v2 <  v1：发生至少一次重置，观测区间增量下界 = v2
//     （上次观测后立刻清零，这是与 Prometheus rate 一致的点估计）。
//  2. 点估计始终是真实增长的严格下界（不引入任何假设）。
//  3. 没有容量信息时，区间内可能发生任意次重置/回绕，真实增量无上界，
//     报告 upper=null 并在 BoundsBasis 中说明。
//  4. 给定容量 C（计数器溢出前的最大计数值）并额外假设“每个观测区间至多
//     重置一次、无隐藏回绕”，才给出条件上界：
//     - 重置区间：C - v1 + v2
//     - 非重置区间：C - v1 + v2（等价于 v2-v1 + C）
//     该值仅在上述假设下成立，字段与说明中均显式标注为条件上界。
//  5. 缺样区间（间隔大于中位间隔 * GapFactor）只标记，不插补、不虚构增长。
//  6. 查询窗口边界不外推为精确值：提供 none / linear / clamped 三种策略，
//     其中 linear/clamped 均是“按观测斜率外推”的启发式估计，不是精确结果。
package rate

import (
	"encoding/json"
	"fmt"
	"math"
	"sort"

	"counterreset/internal/series"
)

// GapFactor：相邻样本间隔超过全序列中位间隔的 1.5 倍即标记为缺样区间。
const GapFactor = 1.5

// Extrapolation 是查询窗口边界的处理策略。
type Extrapolation string

const (
	// ExtrapNone 不外推：只累加完全落在窗口内（含端点）的观测区间增量，
	// 同时返回 observedCoverageMs / windowMs 让调用方自行判断覆盖比例。
	ExtrapNone Extrapolation = "none"
	// ExtrapLinear 线性外推：假设窗口内速率等于观测区间的平均速率，
	// 直接按 window/observed 比例放大。无界的上界仍为 null。
	ExtrapLinear Extrapolation = "linear"
	// ExtrapClamped 截断线性外推（近似 Prometheus extrapolatedRate）：
	// 以平均采样间隔为基准，两端最多各补 1.1 倍平均间隔；边界距离超过
	// 1.1 倍平均间隔的一端只补半个平均间隔，防止长缺口被过度外推。
	ExtrapClamped Extrapolation = "clamped"
)

// ParseExtrapolation 解析查询参数，默认 clamped。
func ParseExtrapolation(s string) (Extrapolation, error) {
	switch Extrapolation(s) {
	case "", ExtrapClamped:
		return ExtrapClamped, nil
	case ExtrapNone:
		return ExtrapNone, nil
	case ExtrapLinear:
		return ExtrapLinear, nil
	default:
		return "", fmt.Errorf("未知 extrapolation 策略 %q（可选 none|linear|clamped）", s)
	}
}

// IntervalInfo 是单个相邻样本区间的手算明细。
type IntervalInfo struct {
	T1Ms       int64    `json:"t1_ms"`
	T2Ms       int64    `json:"t2_ms"`
	V1         float64  `json:"v1"`
	V2         float64  `json:"v2"`
	DeltaMs    int64    `json:"delta_ms"`
	IncreaseLb float64  `json:"increase_lb"` // 严格下界（=点估计）
	IncreaseUb *float64 `json:"increase_ub"` // 条件上界，无容量时为 null
	Reset      bool     `json:"reset"`
	GapRatio   float64  `json:"gap_ratio"` // 区间间隔 / 中位间隔
	IsGap      bool     `json:"is_gap"`
}

// ResetInfo 记录一次被观测到的重置。
type ResetInfo struct {
	BeforeTsMs int64   `json:"before_ts_ms"`
	AfterTsMs  int64   `json:"after_ts_ms"`
	BeforeVal  float64 `json:"before_value"`
	AfterVal   float64 `json:"after_value"`
}

// GapInfo 记录一个缺样区间。
type GapInfo struct {
	FromTsMs int64   `json:"from_ts_ms"`
	ToTsMs   int64   `json:"to_ts_ms"`
	DeltaMs  int64   `json:"delta_ms"`
	MedianMs float64 `json:"median_interval_ms"`
	GapRatio float64 `json:"gap_ratio"`
	Note     string  `json:"note"`
}

// Bounds 是估计范围。Point 为严格下界点估计；
// Upper 为条件上界（可能为 +Inf 语义 → JSON null）。
type Bounds struct {
	Point float64  `json:"point"` // 点估计 = 严格下界
	Lower float64  `json:"lower"` // 同 point：真实增量不可能更低
	Upper *float64 `json:"upper"` // 条件上界；null 表示无容量信息、上界未知
}

// BoundsJSON 与 Bounds 字段一致，仅用于把 +Inf 显式序列化为 null。
type boundsWire struct {
	Point float64  `json:"point"`
	Lower float64  `json:"lower"`
	Upper *float64 `json:"upper"`
}

// MarshalJSON 防御性处理：即便内部 Upper 指向 +Inf，也输出 null。
func (b Bounds) MarshalJSON() ([]byte, error) {
	w := boundsWire{Point: b.Point, Lower: b.Lower, Upper: b.Upper}
	if w.Upper != nil && math.IsInf(*w.Upper, 1) {
		w.Upper = nil
	}
	return json.Marshal(w)
}

// Report 是一次窗口查询的完整结果，包含可手算的中间量。
type Report struct {
	WindowStartMs int64 `json:"window_start_ms"`
	WindowEndMs   int64 `json:"window_end_ms"`
	WindowMs      int64 `json:"window_ms"`

	SelectedSamples []series.Sample `json:"selected_samples"`
	SampleCount     int             `json:"sample_count"`

	MedianIntervalMs float64 `json:"median_interval_ms"`
	ObservedStartMs  int64   `json:"observed_start_ms"`
	ObservedEndMs    int64   `json:"observed_end_ms"`
	ObservedSpanMs   int64   `json:"observed_span_ms"`

	Intervals []IntervalInfo `json:"intervals"`
	Resets    []ResetInfo    `json:"resets"`
	Gaps      []GapInfo      `json:"gaps"`

	Extrapolation Extrapolation `json:"extrapolation"`
	Capacity      *float64      `json:"capacity"` // null 表示未提供容量

	// Raw 是“只基于观测区间”的增长范围（窗口边界不外推）。
	Raw Bounds `json:"raw_increase"`

	// Increase 是应用边界策略后的窗口增长估计范围。
	Increase Bounds `json:"increase"`
	// IncreaseDenominatorMs：速率分母所采用的时长，
	// none/linear/clamped 下恒为 windowMs（见 RatePerSecond）。
	IncreaseDenominatorMs int64 `json:"increase_denominator_ms"`
	// ExtrapolationFactor = 放大后增量 / 原始增量（不外推时为 1）。
	ExtrapolationFactor float64 `json:"extrapolation_factor"`
	// ObservedCoverage = 观测跨度 / 窗口跨度（none 策略下用于提示覆盖不足）。
	ObservedCoverage float64 `json:"observed_coverage"`

	// RatePerSecond 是 increase / windowMs*1000 的范围。
	RatePerSecond Bounds `json:"rate_per_second"`

	// BoundsBasis 用文字说明上下界成立所依赖的假设。
	BoundsBasis string   `json:"bounds_basis"`
	Notes       []string `json:"notes"`
}

func floatPtr(f float64) *float64 { return &f }

func medianInterval(sorted []series.Sample) float64 {
	if len(sorted) < 2 {
		return 0
	}
	deltas := make([]float64, 0, len(sorted)-1)
	for i := 1; i < len(sorted); i++ {
		deltas = append(deltas, float64(sorted[i].TimestampMs-sorted[i-1].TimestampMs))
	}
	sort.Float64s(deltas)
	n := len(deltas)
	if n%2 == 1 {
		return deltas[n/2]
	}
	return (deltas[n/2-1] + deltas[n/2]) / 2
}

// Compute 对已归一化（升序、去重、合法）的样本计算窗口报告。
// capacity <= 0 表示未知容量（不计算条件上界）。
func Compute(samples []series.Sample, startMs, endMs int64, extrap Extrapolation, capacity float64) (*Report, error) {
	if endMs <= startMs {
		return nil, fmt.Errorf("窗口非法：end_ms(%d) 必须大于 start_ms(%d)", endMs, startMs)
	}
	if extrap == "" {
		extrap = ExtrapClamped
	}

	// 选取闭区间 [start, end] 内的样本；输入假定已升序。
	sel := make([]series.Sample, 0)
	for _, s := range samples {
		if s.TimestampMs >= startMs && s.TimestampMs <= endMs {
			sel = append(sel, s)
		}
	}

	r := &Report{
		WindowStartMs:   startMs,
		WindowEndMs:     endMs,
		WindowMs:        endMs - startMs,
		SelectedSamples: sel,
		SampleCount:     len(sel),
		Extrapolation:   extrap,
		Notes:           []string{},
		Intervals:       []IntervalInfo{},
		Resets:          []ResetInfo{},
		Gaps:            []GapInfo{},
	}
	if capacity > 0 {
		r.Capacity = floatPtr(capacity)
	}

	if len(sel) < 2 {
		r.Notes = append(r.Notes,
			"窗口内少于 2 个样本：无法估计任何区间增长，point/lower=0 仅表示“无观测证据”，不代表真实增长为零")
		r.BoundsBasis = boundsBasisText(capacity)
		return r, nil
	}

	med := medianInterval(sel)
	r.MedianIntervalMs = med
	r.ObservedStartMs = sel[0].TimestampMs
	r.ObservedEndMs = sel[len(sel)-1].TimestampMs
	r.ObservedSpanMs = r.ObservedEndMs - r.ObservedStartMs

	// 逐区间累计严格下界与（可能的）条件上界。
	var rawPoint, rawUpper float64
	hasUpper := capacity > 0
	for i := 1; i < len(sel); i++ {
		a, b := sel[i-1], sel[i]
		dt := b.TimestampMs - a.TimestampMs
		reset := b.Value < a.Value

		var incLb float64
		if reset {
			incLb = b.Value // Prometheus 风格：重置后只计新值
		} else {
			incLb = b.Value - a.Value
		}
		rawPoint += incLb

		var incUb *float64
		if hasUpper {
			// 条件上界：假设该区间至多一次重置、无隐藏回绕。
			// 在该假设下无论是否观测到下降，上界均为 C - v1 + v2：
			//   不重置：v2-v1（+0C）；至多一次回绕：v2-v1+1C。
			u := capacity - a.Value + b.Value
			incUb = floatPtr(u)
			rawUpper += u
		}

		ratio := 0.0
		if med > 0 {
			ratio = float64(dt) / med
		}
		isGap := med > 0 && float64(dt) > med*GapFactor
		iv := IntervalInfo{
			T1Ms: a.TimestampMs, T2Ms: b.TimestampMs,
			V1: a.Value, V2: b.Value, DeltaMs: dt,
			IncreaseLb: incLb, IncreaseUb: incUb,
			Reset: reset, GapRatio: ratio, IsGap: isGap,
		}
		r.Intervals = append(r.Intervals, iv)

		if reset {
			r.Resets = append(r.Resets, ResetInfo{
				BeforeTsMs: a.TimestampMs, AfterTsMs: b.TimestampMs,
				BeforeVal: a.Value, AfterVal: b.Value,
			})
		}
		if isGap {
			r.Gaps = append(r.Gaps, GapInfo{
				FromTsMs: a.TimestampMs, ToTsMs: b.TimestampMs, DeltaMs: dt,
				MedianMs: med, GapRatio: ratio,
				Note: "缺样区间：仅依据两个端点计算增量下界，不对区间内部行为作任何插补；区间内可能存在未观测到的重置",
			})
		}
	}

	r.Raw = Bounds{Point: rawPoint, Lower: rawPoint}
	if hasUpper {
		r.Raw.Upper = floatPtr(rawUpper)
	}

	// 边界外推。
	var factor float64 = 1
	switch extrap {
	case ExtrapNone:
		factor = 1
	case ExtrapLinear:
		factor = float64(r.WindowMs) / float64(r.ObservedSpanMs)
	case ExtrapClamped:
		factor = clampedFactor(sel, med, startMs, endMs)
	default:
		return nil, fmt.Errorf("内部错误：未知策略 %q", extrap)
	}
	r.ExtrapolationFactor = factor
	if r.ObservedSpanMs > 0 {
		r.ObservedCoverage = float64(r.ObservedSpanMs) / float64(r.WindowMs)
	}

	point := rawPoint * factor
	r.Increase = Bounds{Point: point, Lower: point}
	if hasUpper {
		up := rawUpper * factor
		r.Increase.Upper = floatPtr(up)
	}
	r.IncreaseDenominatorMs = r.WindowMs

	// 速率统一以窗口时长为分母（每秒）。
	secs := float64(r.WindowMs) / 1000.0
	rp := point / secs
	r.RatePerSecond = Bounds{Point: rp, Lower: rp}
	if hasUpper {
		ru := rawUpper * factor / secs
		r.RatePerSecond.Upper = floatPtr(ru)
	}

	if extrap != ExtrapNone {
		r.Notes = append(r.Notes, fmt.Sprintf(
			"边界采用 %s 外推（因子 %.4f）：这是按观测斜率推算的启发式估计，不是未观测区间的精确值",
			extrap, factor))
	} else {
		r.Notes = append(r.Notes, "边界策略 none：未对窗口两端的未观测部分做外推，注意 observed_coverage 可能小于 1")
	}
	if len(r.Gaps) > 0 {
		r.Notes = append(r.Notes, fmt.Sprintf(
			"存在 %d 个缺样区间（间隔 > 中位间隔的 %.1f 倍），缺样区间内部不插补、不虚构增长",
			len(r.Gaps), GapFactor))
	}
	if len(r.Resets) > 0 {
		r.Notes = append(r.Notes, fmt.Sprintf(
			"观测到 %d 次计数器重置（相邻样本值下降）；点估计假设重置发生在上一样本之后（下界）",
			len(r.Resets)))
	}
	r.BoundsBasis = boundsBasisText(capacity)
	return r, nil
}

// clampedFactor 近似 Prometheus client_golang 的 extrapolatedRate 截断逻辑：
// 平均采样间隔 avg=span/(n-1)，阈值为 1.1*avg；窗口边界距离超过阈值的一端
// 只补 avg/2，否则按实际距离补（上限 1.1*avg）。
func clampedFactor(sel []series.Sample, _ float64, winStart, winEnd int64) float64 {
	first, last := sel[0].TimestampMs, sel[len(sel)-1].TimestampMs
	span := float64(last - first)
	if span <= 0 {
		return 1
	}
	avg := span / float64(len(sel)-1)
	threshold := avg * 1.1

	toStart := float64(first - winStart)
	toEnd := float64(winEnd - last)
	if toStart >= threshold {
		toStart = avg / 2
	}
	if toEnd >= threshold {
		toEnd = avg / 2
	}
	toStart = math.Min(toStart, threshold)
	toEnd = math.Min(toEnd, threshold)
	return (span + toStart + toEnd) / span
}

func boundsBasisText(capacity float64) string {
	base := "point/lower 为严格下界：真实增量不可能更低（即使缺样区间内有隐藏重置，也只会让真实值更大）。"
	if capacity <= 0 {
		return base + "未提供计数器容量，区间内可能发生任意次重置/回绕，真实增量无上界（upper=null）。"
	}
	return fmt.Sprintf(
		"%supper 为条件上界：仅在给定容量 C=%.0f、且每个观测区间至多重置一次、无隐藏回绕的额外假设下成立；缺样区间内部若发生多次重置，上界将失效。",
		base, capacity)
}
