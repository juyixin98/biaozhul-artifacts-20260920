// Package synth 生成可复现的合成指标样本，全部为本地构造，无外部平台依赖。
package synth

import (
	"math"
	"math/rand"

	"metricsink/internal/model"
)

// Density 描述样本密度模式。
type Density string

const (
	// Dense：约每 10s 一个样本（分钟桶内多个样本）。
	Dense Density = "dense"
	// Sparse：约每 90s 一个样本（有些分钟桶为空、小时桶非空）。
	Sparse Density = "sparse"
	// Ragged：不规则间隔（5s~200s），刻意制造跨层边界与空桶。
	Ragged Density = "ragged"
	// Gappy：正常密度，但整段时间无数据（模拟停机缺口）。
	Gappy Density = "gappy"
)

// Options 控制一段合成序列的生成。
type Options struct {
	Metric   string
	Labels   map[string]string
	IDPrefix string
	Start    int64 // Unix 秒
	End      int64 // Unix 秒
	Density  Density
	Seed     int64
}

// Generate 返回确定性的样本序列（相同入参得到相同数据）。
// 数值由“日周期正弦 + 线性趋势 + 噪声”组成，取值有界且非 NaN。
func Generate(o Options) []model.Sample {
	rng := rand.New(rand.NewSource(o.Seed))
	var samples []model.Sample

	emit := func(ts int64) {
		phase := float64(ts%3600) / 3600.0 * 2 * math.Pi
		base := 50 + 20*math.Sin(phase)
		trend := float64(ts-o.Start) / 3600.0
		noise := rng.NormFloat64() * 3
		v := base + trend + noise
		id := o.IDPrefix
		if id != "" {
			id += "-"
		}
		id += itoa(ts)
		samples = append(samples, model.Sample{
			ID:     id,
			Metric: o.Metric,
			Labels: cloneLabels(o.Labels),
			Ts:     ts,
			Value:  v,
		})
	}

	switch o.Density {
	case Dense:
		for ts := o.Start; ts <= o.End; ts += 10 {
			emit(ts)
		}
	case Sparse:
		for ts := o.Start; ts <= o.End; ts += 90 {
			emit(ts)
		}
	case Ragged:
		ts := o.Start
		for ts <= o.End {
			emit(ts)
			ts += 5 + rng.Int63n(196)
		}
	case Gappy:
		// 前 1/3 与后 1/3 有数据，中间 1/3 完全空白。
		gapStart := o.Start + (o.End-o.Start)/3
		gapEnd := o.Start + 2*(o.End-o.Start)/3
		for ts := o.Start; ts <= o.End; ts += 20 {
			if ts >= gapStart && ts <= gapEnd {
				continue
			}
			emit(ts)
		}
	default:
		for ts := o.Start; ts <= o.End; ts += 30 {
			emit(ts)
		}
	}
	return samples
}

// Outlier 在给定序列里把某一样本替换成极值，模拟迟到的极端值修订。
func Outlier(samples []model.Sample, idx int, value float64) model.Sample {
	old := samples[idx]
	old.Value = value
	samples[idx] = old
	return old
}

// ReviseCopy 返回样本的一份“修订副本”（同 ID，可改时间/数值）。
func ReviseCopy(s model.Sample, newTs int64, newValue float64) model.Sample {
	s.Ts = newTs
	s.Value = newValue
	return s
}

func cloneLabels(in map[string]string) map[string]string {
	out := make(map[string]string, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

func itoa(n int64) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		b[i] = '-'
	}
	return string(b[i:])
}
