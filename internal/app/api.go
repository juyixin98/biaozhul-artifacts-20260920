// Package app 负责把 JSON 请求装配成“离散事件引擎 + 网络 + 银行节点 + 快照管理器”，
// 运行后输出可序列化的 JSON 结果。
package app

import (
	"errors"
	"fmt"

	"simsnap/internal/sim"
)

// LinkSpec 指定一条链路及其策略。
type LinkSpec struct {
	A      int        `json:"a"`
	B      int        `json:"b"`
	Policy sim.Policy `json:"policy"`
}

// TransferEvent 是脚本化的一笔转账：At 时刻 From -> To，金额 Amount。
type TransferEvent struct {
	At     int64 `json:"at"`
	From   int   `json:"from"`
	To     int   `json:"to"`
	Amount int   `json:"amount"`
}

// SnapshotEvent 是脚本化的一次快照发起。
type SnapshotEvent struct {
	At        int64 `json:"at"`
	ID        int   `json:"id"`
	Initiator int   `json:"initiator"`
}

// TrafficSpec 请求在 [From, Until] 内每 Every 个时间单位发起一笔随机转账
// （随机源来自引擎种子，因此完全可复现）。
type TrafficSpec struct {
	From      int64 `json:"from"`
	Until     int64 `json:"until"`
	Every     int64 `json:"every"`
	MaxAmount int   `json:"max_amount"`
}

// Request 是一次模拟运行的完整输入。
type Request struct {
	Seed          int64           `json:"seed"`
	Balances      []int           `json:"balances"`       // 各节点初始余额（长度即节点数）
	DefaultPolicy *sim.Policy     `json:"default_policy"` // 未在 links 中显式给出的链路使用此策略
	Links         []LinkSpec      `json:"links,omitempty"`
	Transfers     []TransferEvent `json:"transfers,omitempty"`
	Snapshots     []SnapshotEvent `json:"snapshots,omitempty"`
	Traffic       *TrafficSpec    `json:"traffic,omitempty"`
	Deadline      int64           `json:"deadline,omitempty"` // 仿真截止时刻；0 表示跑到事件队列清空
}

// Validate 校验请求并返回归一化后的每条链路策略（键 "a-b", a<b）。
func (r *Request) Validate() (map[string]sim.Policy, error) {
	n := len(r.Balances)
	if n < 2 {
		return nil, errors.New("balances 至少需要 2 个节点")
	}
	for i, b := range r.Balances {
		if b < 0 {
			return nil, fmt.Errorf("balances[%d] 不能为负", i)
		}
	}
	if r.DefaultPolicy == nil && len(r.Links) == 0 {
		return nil, errors.New("必须提供 default_policy 或 links")
	}
	if r.DefaultPolicy != nil {
		if err := validatePolicy(*r.DefaultPolicy); err != nil {
			return nil, fmt.Errorf("default_policy: %w", err)
		}
	}

	out := map[string]sim.Policy{}
	if r.DefaultPolicy != nil {
		for a := 0; a < n; a++ {
			for b := a + 1; b < n; b++ {
				out[pairKey(a, b)] = *r.DefaultPolicy
			}
		}
	}
	seen := map[string]bool{}
	for _, l := range r.Links {
		if l.A < 0 || l.A >= n || l.B < 0 || l.B >= n {
			return nil, fmt.Errorf("link %d-%d 节点越界", l.A, l.B)
		}
		if l.A == l.B {
			return nil, errors.New("不允许自环链路")
		}
		key := pairKey(l.A, l.B)
		if seen[key] {
			return nil, fmt.Errorf("link %d-%d 重复指定", l.A, l.B)
		}
		seen[key] = true
		if err := validatePolicy(l.Policy); err != nil {
			return nil, fmt.Errorf("link %d-%d: %w", l.A, l.B, err)
		}
		out[key] = l.Policy
	}
	// default 与显式 links 合并后仍需覆盖全互联拓扑。
	for a := 0; a < n; a++ {
		for b := a + 1; b < n; b++ {
			if _, ok := out[pairKey(a, b)]; !ok {
				return nil, fmt.Errorf("节点 %d 与 %d 之间缺少链路策略（且无 default_policy）", a, b)
			}
		}
	}

	seenSnap := map[int]bool{}
	for _, s := range r.Snapshots {
		if s.ID < 1 {
			return nil, errors.New("snapshot id 必须 >= 1")
		}
		if seenSnap[s.ID] {
			return nil, fmt.Errorf("snapshot id %d 重复", s.ID)
		}
		seenSnap[s.ID] = true
		if s.Initiator < 0 || s.Initiator >= n {
			return nil, fmt.Errorf("snapshot %d initiator 越界", s.ID)
		}
	}
	for _, t := range r.Transfers {
		if t.From < 0 || t.From >= n || t.To < 0 || t.To >= n {
			return nil, errors.New("transfer 节点越界")
		}
		if t.From == t.To {
			return nil, errors.New("不允许自己给自己转账")
		}
		if t.Amount <= 0 {
			return nil, errors.New("transfer 金额必须为正")
		}
	}
	if r.Traffic != nil {
		t := r.Traffic
		if t.Every < 1 {
			return nil, errors.New("traffic.every 必须 >= 1")
		}
		if t.MaxAmount < 1 {
			return nil, errors.New("traffic.max_amount 必须 >= 1")
		}
		if t.Until < t.From {
			return nil, errors.New("traffic.until 不能小于 from")
		}
		if r.Deadline <= 0 {
			return nil, errors.New("启用持续流量 traffic 时必须设置 deadline 以保证运行终止")
		}
	}
	return out, nil
}

func validatePolicy(p sim.Policy) error {
	if p.MinDelay < 1 {
		return errors.New("min_delay 必须 >= 1")
	}
	if p.MaxDelay < p.MinDelay {
		return errors.New("max_delay 不能小于 min_delay")
	}
	if p.LossPct < 0 || p.LossPct > 100 || p.DupPct < 0 || p.DupPct > 100 {
		return errors.New("loss_pct/dup_pct 必须在 0..100")
	}
	return nil
}

func pairKey(a, b int) string {
	if a > b {
		a, b = b, a
	}
	return fmt.Sprintf("%d-%d", a, b)
}
