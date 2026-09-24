package scaler

import "math"

// Scenario 是一个离线验收场景：名称、说明与回放输入。
type Scenario struct {
	Name        string        `json:"name"`
	Description string        `json:"description"`
	Request     ReplayRequest `json:"request"`
}

// 三个场景共用一套策略参数，区别只在负载曲线。
func defaultScenarioConfig() Config {
	return Config{
		MinReplicas:                 2,
		MaxReplicas:                 10,
		LoadPerReplica:              100,
		UpWindowSeconds:             60,
		DownWindowSeconds:           300,
		UpThresholdPercent:          10,
		DownThresholdPercent:        20,
		MaxUpStep:                   2,
		MaxDownStep:                 1,
		UpCooldownSeconds:           60,
		DownCooldownSeconds:         120,
		ScaleUpStabilizationSeconds: 180,
		ColdStartDelaySeconds:       120,
		FreshnessSeconds:            45,
		MaxSampleGapSeconds:         45,
	}
}

func samplesEvery30(last int64, load func(t int64) float64, missing func(t int64) bool) []Sample {
	var out []Sample
	for t := int64(0); t <= last; t += 30 {
		s := Sample{TimeSeconds: t}
		if missing != nil && missing(t) {
			s.Missing = true
		} else {
			s.TotalLoad = load(t)
		}
		out = append(out, s)
	}
	return out
}

// SpikeScenario 场景一：突发尖峰 + 持续高负载平台 + 回落。
//
// 负载曲线：
//   - t=0~299     低负载 150，期间 t=30/90 有两次 900 的孤立尖峰
//     （落在初始冷启动窗口内，门禁不允许任何动作）；
//   - t=300~419   高负载平台 850（期望 9 副本，当前 2 副本）；
//   - t>=420      回落到 150。
//
// 用于验证：
//   - 冷启动窗口内即使出现尖峰也不动作；
//   - 高平台期间单步限速 +2 与扩容冷启动（pending 期间不决策）交替，
//     多拍才能逼近期望副本；
//   - 回落后 300s 缩容稳定窗口内拒绝缩容，扩容后稳定窗口再次拦截；
//   - 允许缩容后，缩容冷却 120s 与单步限速 -1 共同决定缓慢回落。
func SpikeScenario() Scenario {
	spikes := map[int64]bool{30: true, 90: true}
	samples := samplesEvery30(1440, func(t int64) float64 {
		switch {
		case spikes[t]:
			return 900
		case t >= 300 && t < 420:
			return 850
		default:
			return 150
		}
	}, nil)
	return Scenario{
		Name:        "spike-突发尖峰",
		Description: "冷启动期出现两次 900 孤立尖峰（门禁拦截），t=300~419 为 850 高负载平台，t=420 起回落到 150；验证冷启动门禁、扩容限速+冷启动、缩容稳定窗口、缩容冷却与 -1 限速。",
		Request: ReplayRequest{
			Config:              defaultScenarioConfig(),
			DecisionIntervalSec: 60,
			Samples:             samples,
		},
	}
}

// RampScenario 场景二：持续增长后跌落。
//
// 负载从 100 起以 1.5/s 持续增长，t=1200 达到 1900（对应 19 副本，
// 远超 maxReplicas=10）后立刻跌回 100 并长期保持。
// 用于验证：
//   - 持续增长下多拍逐步扩容、每拍 +2 限速；
//   - maxReplicas=10 上限钳制（期望超过 10 时钳住，理由中注明）；
//   - 跌落后必须等完整 300s 缩容窗口全部被低负载覆盖才开始缩容；
//   - 缩容冷却下每 120s 才允许 -1，逐步回到 minReplicas。
func RampScenario() Scenario {
	samples := samplesEvery30(2880, func(t int64) float64 {
		if t <= 1200 {
			return float64(100) + 1.5*float64(t)
		}
		return 100
	}, nil)
	return Scenario{
		Name:        "ramp-持续增长",
		Description: "负载 100+1.5t 持续增长到 1900（期望 19 副本，上限 10）后跌回 100；验证逐步扩容、上限钳制、300s 缩容稳定窗口与缩容冷却。",
		Request: ReplayRequest{
			Config:              defaultScenarioConfig(),
			DecisionIntervalSec: 60,
			Samples:             samples,
		},
	}
}

// OscillationScenario 场景三：周期振荡 + 采集中断。
//
// 负载按 720s 周期在 150~650 之间正弦振荡；t∈[600,720) 采集中断
// （样本显式缺失，而非零负载）。用于验证：
//   - 波峰到来时按 +2 限速逐步扩容；
//   - 迟滞带吸收正常振荡，波谷也不立刻缩容；
//   - 缺失样本期间最新信号失效 → 保持副本（绝不按零负载缩容）；
//   - 恢复后，缩容窗口因包含缺失样本而在多个决策拍上判定不完整，拒绝缩容。
func OscillationScenario() Scenario {
	samples := samplesEvery30(1500, func(t int64) float64 {
		return 400 - 250*math.Cos(2*math.Pi*float64(t)/720.0)
	}, func(t int64) bool {
		return t >= 600 && t < 720
	})
	return Scenario{
		Name:        "oscillation-周期振荡与采集中断",
		Description: "720s 周期、150~650 振幅的正弦负载，t∈[600,720) 指标缺失；验证迟滞吸振、缺失不缩容（不当零负载）与缩容窗口完整性保护。",
		Request: ReplayRequest{
			Config:              defaultScenarioConfig(),
			DecisionIntervalSec: 60,
			Samples:             samples,
		},
	}
}

// AllScenarios 返回全部验收场景。
func AllScenarios() []Scenario {
	return []Scenario{SpikeScenario(), RampScenario(), OscillationScenario()}
}
