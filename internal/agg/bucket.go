// Package agg 定义降采样桶的聚合原语。
//
// 核心约束：每层桶都保存 count/sum/min/max 四个充分统计量，
// 高层桶永远由低层桶（或原始样本）的这四个量合并而来，
// 绝不从平均值再做平均。当各桶样本数不同时，
// (a1+a2+...)/n ≠ mean(桶均值)，只存平均值会丢失权重。
package agg

import "math"

// Bucket 是一个时间桶上的聚合状态。
// Count==0 表示空桶，Sum/Min/Max 此时无意义。
type Bucket struct {
	Count int64   `json:"count"`
	Sum   float64 `json:"sum"`
	Min   float64 `json:"min"`
	Max   float64 `json:"max"`
}

// Add 把一个原始样本并入桶。
func (b *Bucket) Add(v float64) {
	if b.Count == 0 {
		b.Min = v
		b.Max = v
	} else {
		if v < b.Min {
			b.Min = v
		}
		if v > b.Max {
			b.Max = v
		}
	}
	b.Sum += v
	b.Count++
}

// Merge 把另一个桶（更细粒度层）的充分统计量合并进来。
// 这是“不从平均值再平均”的关键：sum 相加、count 相加、
// min 取更小、max 取更大。
func (b *Bucket) Merge(o Bucket) {
	if o.Count == 0 {
		return
	}
	if b.Count == 0 {
		*b = o
		return
	}
	b.Count += o.Count
	b.Sum += o.Sum
	if o.Min < b.Min {
		b.Min = o.Min
	}
	if o.Max > b.Max {
		b.Max = o.Max
	}
}

// Avg 返回均值；空桶返回 NaN。
func (b Bucket) Avg() float64 {
	if b.Count == 0 {
		return math.NaN()
	}
	return b.Sum / float64(b.Count)
}

// AlmostEqual 用于测试中与原始重算结果比对。
// count 必须精确相等；sum/min/max 允许浮点容差。
func (b Bucket) AlmostEqual(o Bucket, eps float64) bool {
	if b.Count != o.Count {
		return false
	}
	if b.Count == 0 {
		return true
	}
	return floatEq(b.Sum, o.Sum, eps) &&
		floatEq(b.Min, o.Min, eps) &&
		floatEq(b.Max, o.Max, eps)
}

func floatEq(a, b, eps float64) bool {
	if math.IsNaN(a) || math.IsNaN(b) {
		return math.IsNaN(a) && math.IsNaN(b)
	}
	d := math.Abs(a - b)
	scale := math.Max(1, math.Max(math.Abs(a), math.Abs(b)))
	return d <= eps*scale
}
