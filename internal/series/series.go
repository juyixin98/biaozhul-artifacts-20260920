// Package series 处理计数器样本的基本结构、校验与归一化。
//
// 计数器语义（与 Prometheus 一致）：计数器值是【非负单调累计】的原始值，
// 正常情况下只会增大；一旦观测到比上一个值更小的样本，即判定该区间发生了
// 计数器重置（进程重启 / 进程外清零）。重置区间的增长计算由 rate 包负责。
package series

import (
	"errors"
	"math"
	"sort"
)

// Sample 是一个计数器采样点：毫秒级 Unix 时间戳 + 非负累计值。
type Sample struct {
	TimestampMs int64   `json:"ts_ms"`
	Value       float64 `json:"value"`
}

// 输入校验相关错误。
var (
	ErrNegativeValue = errors.New("计数器值不允许为负：计数器语义下原始值必须非负")
	ErrNonFinite     = errors.New("计数器值必须是有限数：拒绝 NaN / +Inf / -Inf")
	ErrBadTimestamp  = errors.New("时间戳必须为正整数（毫秒级 Unix 时间）")
)

// Validate 校验单个样本。负值是明确的语义错误，直接拒绝；
// NaN/Inf 等非有限值同样拒绝（无法参与任何可靠计算）。
func (s Sample) Validate() error {
	if math.IsNaN(s.Value) || math.IsInf(s.Value, 0) {
		return ErrNonFinite
	}
	if s.Value < 0 {
		return ErrNegativeValue
	}
	if s.TimestampMs <= 0 {
		return ErrBadTimestamp
	}
	return nil
}

// Rejected 记录一次批量摄入中被拒绝的样本及其原因，便于调用方定位。
type Rejected struct {
	Index int     `json:"index"` // 在请求数组中的下标
	TsMs  int64   `json:"ts_ms"`
	Value float64 `json:"value,omitempty"`
	Error string  `json:"error"`
}

// ValidateBatch 校验整批样本；只要有一个非法就返回全部拒绝明细，
// 由上层决定是否整批拒绝（HTTP API 采用整批拒绝，不做部分写入）。
func ValidateBatch(samples []Sample) []Rejected {
	var rejected []Rejected
	for i, s := range samples {
		if err := s.Validate(); err != nil {
			rejected = append(rejected, Rejected{
				Index: i,
				TsMs:  s.TimestampMs,
				Value: s.Value,
				Error: err.Error(),
			})
		}
	}
	return rejected
}

// NormalizeResult 是归一化结果统计。
type NormalizeResult struct {
	Samples           []Sample // 按时间戳升序、每个时间戳只保留一个值
	DuplicateSame     int      // 同时间戳同值的重复样本数
	DuplicateConflict int      // 同时间戳不同值的冲突样本组数
}

// Normalize 对样本做：
//  1. 升序排序（处理乱序到达）；
//  2. 同时间戳去重：值相同算幂等重复，值不同采用 max-wins 策略
//     （计数器“大值”是更晚的累计观测；冲突单独计数暴露给调用方）。
//
// 不假设调用方已经过滤非法值，调用前应先通过 ValidateBatch。
func Normalize(in []Sample) NormalizeResult {
	sorted := make([]Sample, len(in))
	copy(sorted, in)
	// 主序按时间戳；同时间戳把大值排前面，方便遍历时直接保留第一个。
	sort.SliceStable(sorted, func(i, j int) bool {
		if sorted[i].TimestampMs != sorted[j].TimestampMs {
			return sorted[i].TimestampMs < sorted[j].TimestampMs
		}
		return sorted[i].Value > sorted[j].Value
	})

	out := make([]Sample, 0, len(sorted))
	res := NormalizeResult{}
	for _, s := range sorted {
		if n := len(out); n > 0 && out[n-1].TimestampMs == s.TimestampMs {
			kept := out[n-1].Value // 排序保证 kept >= s.Value
			if kept == s.Value {
				res.DuplicateSame++
			} else {
				res.DuplicateConflict++
			}
			continue
		}
		out = append(out, s)
	}
	res.Samples = out
	return res
}
