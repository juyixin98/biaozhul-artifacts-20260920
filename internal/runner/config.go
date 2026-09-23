package runner

import (
	"fmt"

	"twopcsim/internal/twopc"
)

// TimingsForTest 返回测试用的定时器配置（窗口适中、重发积极）。
func TimingsForTest() twopc.Timings {
	return twopc.Timings{VoteTimeout: 60, Resend: 12, Query: 8}
}

func withDefaults(sc Scenario) Scenario {
	if sc.MaxTick <= 0 {
		sc.MaxTick = 300
	}
	if sc.Timings.VoteTimeout <= 0 {
		sc.Timings.VoteTimeout = twopc.DefaultTimings().VoteTimeout
	}
	if sc.Timings.Resend <= 0 {
		sc.Timings.Resend = twopc.DefaultTimings().Resend
	}
	if sc.Timings.Query <= 0 {
		sc.Timings.Query = twopc.DefaultTimings().Query
	}
	n := &sc.Network
	if n.MinDelay <= 0 {
		n.MinDelay = 1
	}
	if n.MaxDelay <= 0 {
		n.MaxDelay = 4
	}
	if n.MaxDelay < n.MinDelay {
		n.MaxDelay = n.MinDelay
	}
	if n.ReorderDelay <= 0 {
		n.ReorderDelay = 8
	}
	return sc
}

func validate(sc Scenario) error {
	ids := map[string]bool{CoordinatorID: true}
	for _, p := range ParticipantIDs() {
		ids[p] = true
	}
	seenTxn := map[string]bool{}
	for _, t := range sc.Transactions {
		if t.ID == "" {
			return fmt.Errorf("transaction 缺少 id")
		}
		if seenTxn[t.ID] {
			return fmt.Errorf("重复的 transaction id: %s", t.ID)
		}
		seenTxn[t.ID] = true
		for _, v := range t.VoteNo {
			if !ids[v] {
				return fmt.Errorf("voteNo 引用了未知节点: %s", v)
			}
		}
	}
	for _, cr := range sc.Crashes {
		if !ids[cr.NodeID] {
			return fmt.Errorf("crash 规则引用未知节点: %s", cr.NodeID)
		}
		if cr.Hook == "" && cr.AtTick <= 0 {
			return fmt.Errorf("crash 规则必须指定 hook 或正的 atTick: %+v", cr)
		}
		if cr.Hook == "" && cr.RestartTick != 0 && cr.RestartTick <= cr.AtTick {
			return fmt.Errorf("restartTick(%d) 必须晚于崩溃时刻(%d)", cr.RestartTick, cr.AtTick)
		}
	}
	n := sc.Network
	for _, rate := range []float64{n.LossRate, n.DuplicateRate, n.ReorderRate} {
		if rate < 0 || rate > 1 {
			return fmt.Errorf("网络概率必须在 [0,1] 区间")
		}
	}
	return nil
}
