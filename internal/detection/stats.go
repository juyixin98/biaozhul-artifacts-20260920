package detection

import "math"

// meanStd 返回样本均值与样本标准差（n-1 分母）。n<2 时标准差为 0。
func meanStd(xs []float64) (float64, float64) {
	n := float64(len(xs))
	if n == 0 {
		return 0, 0
	}
	sum := 0.0
	for _, x := range xs {
		sum += x
	}
	mean := sum / n
	if n < 2 {
		return mean, 0
	}
	ss := 0.0
	for _, x := range xs {
		d := x - mean
		ss += d * d
	}
	return mean, math.Sqrt(ss / (n - 1))
}

type zDecision struct {
	Triggered    bool    `json:"triggered"`
	Z            float64 `json:"z"`
	Mean         float64 `json:"mean"`
	Std          float64 `json:"std"`
	Current      float64 `json:"current"`
	SampleDays   int     `json:"sample_days"`
	PositiveDays int     `json:"positive_days"`
	Reason       string  `json:"reason,omitempty"` // 样本不足等未触发原因
}

// decideZScore 依据最近 lookbackDays 个历史自然日的每日计数判定当日是否异常。
// history 长度应等于 lookbackDays（无事件的日子以 0 计入），current 为当日计数。
// 样本不足（总天数不足或正样本天数不足）时不触发，由调用方只保留固定规则结果。
func decideZScore(history []float64, current float64, p ZScoreParams) zDecision {
	positive := 0
	for _, v := range history {
		if v > 0 {
			positive++
		}
	}
	d := zDecision{Current: current, SampleDays: len(history), PositiveDays: positive, Mean: 0, Std: 0}

	if len(history) < p.MinSampleDays || positive < p.MinPositiveDays {
		d.Reason = "insufficient_sample"
		return d
	}
	mean, std := meanStd(history)
	d.Mean, d.Std = mean, std

	switch {
	case std > 0:
		d.Z = (current - mean) / std
		if d.Z > p.ZThreshold {
			d.Triggered = true
		}
	case mean > 0 && current > mean:
		// 零方差但当日计数高于历史恒定值，视为显著异常。
		d.Z = math.Inf(1)
		d.Triggered = true
	}
	return d
}
