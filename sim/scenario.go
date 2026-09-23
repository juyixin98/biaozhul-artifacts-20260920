// Package sim 是单进程确定性离散事件模拟器：所有副本在进程内建模，
// 网络行为（丢包、重复、乱序、延迟）完全由场景与种子决定，可重复复放，
// 不依赖任何真实网络、真实时钟或集群设施。
package sim

import (
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"os"
)

// Network 描述不可靠链路的概率模型。所有判定都使用确定性 PRNG。
type Network struct {
	// DropProb 是每条消息被整条丢弃的概率，范围 [0,1)。
	DropProb float64 `json:"drop_prob"`
	// DuplicateProb 是未丢弃消息再额外复制一份的概率，范围 [0,1)。
	DuplicateProb float64 `json:"duplicate_prob"`
	// ReorderProb 是发送时尝试强制“后发先至”的概率，范围 [0,1)。
	// 此外不同消息独立抽取延迟也会自然产生乱序，二者都会被统计。
	ReorderProb float64 `json:"reorder_prob"`
	// MinDelay/MaxDelay 为整数 ticks 的均匀延迟区间（含端点）。
	MinDelay int `json:"min_delay"`
	MaxDelay int `json:"max_delay"`
}

// EventSpec 是输入场景中的一个调度事件（全部发生在离散整数时刻）。
//
// Kind 取值：
//
//	add    在 Replica 本地添加 Value（标签 origin=Replica，序号本地递增）
//	remove 在 Replica 本地对 Value 执行观察删除
//	sync   Replica 向 To 推送一次完整状态（定向）
//	gossip Replica 向其余所有副本各推送一次完整状态（扇出）
type EventSpec struct {
	At      int    `json:"at"`
	Kind    string `json:"kind"`
	Replica string `json:"replica"`
	Value   string `json:"value,omitempty"`
	To      string `json:"to,omitempty"`
}

// Scenario 是 JSON 运行接口的输入结构。
type Scenario struct {
	Name     string      `json:"name"`
	Seed     uint64      `json:"seed"`
	Replicas []string    `json:"replicas"`
	Network  Network     `json:"network"`
	Events   []EventSpec `json:"events"`
	// ExpectedValues 可选：给出充分同步后期望的有效元素集合（升序），
	// 结果中会附带 MatchesExpected 判定。
	ExpectedValues []string `json:"expected_values,omitempty"`
}

// LoadScenario 从 JSON 文件读取并校验场景。
func LoadScenario(path string) (*Scenario, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var sc Scenario
	if err := json.Unmarshal(data, &sc); err != nil {
		return nil, fmt.Errorf("解析场景 JSON: %w", err)
	}
	if err := sc.Validate(); err != nil {
		return nil, err
	}
	return &sc, nil
}

// Validate 检查场景自洽性。
func (sc *Scenario) Validate() error {
	if len(sc.Replicas) == 0 {
		return fmt.Errorf("replicas 不能为空")
	}
	seen := make(map[string]bool, len(sc.Replicas))
	for _, r := range sc.Replicas {
		if r == "" {
			return fmt.Errorf("副本名不能为空")
		}
		if seen[r] {
			return fmt.Errorf("副本名重复: %s", r)
		}
		seen[r] = true
	}
	n := sc.Network
	if n.DropProb < 0 || n.DropProb >= 1 {
		return fmt.Errorf("drop_prob 必须在 [0,1) 内")
	}
	if n.DuplicateProb < 0 || n.DuplicateProb >= 1 {
		return fmt.Errorf("duplicate_prob 必须在 [0,1) 内")
	}
	if n.ReorderProb < 0 || n.ReorderProb >= 1 {
		return fmt.Errorf("reorder_prob 必须在 [0,1) 内")
	}
	if n.MinDelay < 0 || n.MaxDelay < n.MinDelay {
		return fmt.Errorf("要求 max_delay >= min_delay >= 0")
	}
	for i, e := range sc.Events {
		if e.At < 0 {
			return fmt.Errorf("事件 %d: at 不能为负", i)
		}
		switch e.Kind {
		case "add", "remove":
			if !seen[e.Replica] {
				return fmt.Errorf("事件 %d: 未知副本 %q", i, e.Replica)
			}
			if e.Value == "" {
				return fmt.Errorf("事件 %d: %s 需要 value", i, e.Kind)
			}
		case "sync":
			if !seen[e.Replica] || !seen[e.To] {
				return fmt.Errorf("事件 %d: sync 的副本必须存在", i)
			}
			if e.Replica == e.To {
				return fmt.Errorf("事件 %d: sync 不能发给自己", i)
			}
		case "gossip":
			if !seen[e.Replica] {
				return fmt.Errorf("事件 %d: 未知副本 %q", i, e.Replica)
			}
		default:
			return fmt.Errorf("事件 %d: 未知 kind %q（add|remove|sync|gossip）", i, e.Kind)
		}
	}
	return nil
}

// rngFor 由种子派生确定性随机源。
func rngFor(seed uint64) *rand.Rand {
	return rand.New(rand.NewPCG(seed, seed^0x9E3779B97F4A7C15))
}
