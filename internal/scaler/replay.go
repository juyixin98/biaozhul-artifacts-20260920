package scaler

import "fmt"

// ReplayRequest 是离线回放的输入：策略 + 样本 + 决策时刻序列。
type ReplayRequest struct {
	Config              Config   `json:"config"`
	InitialReplicas     int      `json:"initialReplicas"` // 0 表示用 minReplicas
	DecisionTimes       []int64  `json:"decisionTimes"`   // 决策时刻（秒，升序）
	Samples             []Sample `json:"samples"`
	DecisionIntervalSec int64    `json:"decisionIntervalSeconds"` // decisionTimes 为空时按间隔生成
}

// ReplaySummary 汇总一次回放的结果。
type ReplaySummary struct {
	Ticks            int `json:"ticks"`
	ScaleUps         int `json:"scaleUps"`
	ScaleDowns       int `json:"scaleDowns"`
	Holds            int `json:"holds"`
	MissingSignal    int `json:"missingSignalHolds"`
	MaxTotalReplicas int `json:"maxTotalReplicas"`
	FinalReady       int `json:"finalReady"`
	FinalPending     int `json:"finalPending"`
}

// ReplayResult 是回放输出。
type ReplayResult struct {
	Config    Config        `json:"config"`
	Decisions []Decision    `json:"decisions"`
	Summary   ReplaySummary `json:"summary"`
}

// Replay 在给定样本上离线推进控制器，返回每个决策时刻的完整决策与依据。
func Replay(req ReplayRequest) (*ReplayResult, error) {
	cfg := req.Config
	cfg.ApplyDefaults()
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("配置非法: %w", err)
	}
	samples := SortedSamples(req.Samples)
	if err := ValidateSamples(samples); err != nil {
		return nil, fmt.Errorf("样本非法: %w", err)
	}
	times := append([]int64(nil), req.DecisionTimes...)
	if len(times) == 0 {
		if req.DecisionIntervalSec <= 0 {
			return nil, fmt.Errorf("必须提供 decisionTimes 或正数 decisionIntervalSeconds")
		}
		if len(samples) == 0 {
			return nil, fmt.Errorf("无决策时刻且无样本，无法回放")
		}
		for t := samples[0].TimeSeconds; t <= samples[len(samples)-1].TimeSeconds; t += req.DecisionIntervalSec {
			times = append(times, t)
		}
	}
	for i := 1; i < len(times); i++ {
		if times[i] <= times[i-1] {
			return nil, fmt.Errorf("decisionTimes 必须严格升序")
		}
	}

	engine := NewEngine(cfg)
	if req.InitialReplicas > 0 {
		if req.InitialReplicas < cfg.MinReplicas || req.InitialReplicas > cfg.MaxReplicas {
			return nil, fmt.Errorf("initialReplicas=%d 超出 [%d,%d]",
				req.InitialReplicas, cfg.MinReplicas, cfg.MaxReplicas)
		}
		engine.ready = req.InitialReplicas
	}

	res := &ReplayResult{Config: cfg}
	for _, t := range times {
		d := engine.Decide(t, samples)
		res.Decisions = append(res.Decisions, d)
		res.Summary.Ticks++
		switch d.Action {
		case ActionUp:
			res.Summary.ScaleUps++
		case ActionDown:
			res.Summary.ScaleDowns++
		default:
			res.Summary.Holds++
		}
		if !d.SignalOK {
			res.Summary.MissingSignal++
		}
		if total := d.ReadyAfter + d.PendingAfter; total > res.Summary.MaxTotalReplicas {
			res.Summary.MaxTotalReplicas = total
		}
	}
	res.Summary.FinalReady = engine.ready
	res.Summary.FinalPending = engine.pending
	return res, nil
}
