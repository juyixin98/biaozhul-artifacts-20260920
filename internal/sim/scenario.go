// Package sim 是 Raft 节点的确定性离散事件模拟器。
//
// 模拟器维护一个按 (时间, 序号) 排序的事件队列，驱动固定 3 个 Raft
// 节点在单进程内运行；节点之间不直接通信，所有消息都经过可丢包、
// 重复和乱序的虚拟网络。场景通过 JSON 描述，见 examples/ 目录。
package sim

import (
	"encoding/json"
	"fmt"
	"strconv"
)

// Scenario 是一次模拟运行的完整输入。
type Scenario struct {
	Name      string     `json:"name"`
	NodeCount int        `json:"nodeCount"` // 必须为 3
	Seed      int64      `json:"seed"`
	Tick      int64      `json:"tick"`      // 时间单位（毫秒），仅用于文档化
	EndTime   int64      `json:"endTime"`   // 模拟在此时刻停止（含此时刻之前投递的事件）
	Heartbeat int        `json:"heartbeat"` // 心跳周期（毫秒）
	Election  TimeRange  `json:"electionTimeout"`
	Network   NetworkCfg `json:"network"`
	// DataDir 为持久化目录；运行开始时清除其中的节点状态文件，
	// 运行过程中的 restart 事件从同一目录恢复。
	DataDir string `json:"dataDir"`
	// Nodes 可选：按节点 ID 覆盖时序参数（例如固定某节点选举超时最短）。
	Nodes  map[int]NodeCfg `json:"nodes,omitempty"`
	Trace  bool            `json:"trace"`
	Events []ScenarioEvent `json:"events"`
}

// NodeCfg 是单个节点的可选覆盖参数；零值表示沿用全局设置。
type NodeCfg struct {
	ElectionMin int `json:"electionMin,omitempty"`
	ElectionMax int `json:"electionMax,omitempty"`
}

// TimeRange 是随机选举超时的闭区间。
type TimeRange struct {
	Min int `json:"min"`
	Max int `json:"max"`
}

// NetworkCfg 描述虚拟网络的默认行为。
type NetworkCfg struct {
	BaseDelay int64   `json:"baseDelay"` // 每条消息的基础时延（毫秒）
	JitterPct float64 `json:"jitterPct"` // 相对基础时延的随机抖动比例，制造乱序
	LossPct   float64 `json:"lossPct"`   // 每条消息的独立丢包概率
	DupPct    float64 `json:"dupPct"`    // 每条消息额外复制一份的概率
}

// ScenarioEvent 是脚本中的一个外部事件。
type ScenarioEvent struct {
	Time    int64    `json:"time"`
	Type    string   `json:"type"`    // client | crash | restart | partition | heal | link
	Node    *NodeRef `json:"node"`    // client/crash/restart 的目标节点
	Client  string   `json:"client"`  // client: 客户端请求标识
	Command string   `json:"command"` // client: 写入内容
	Groups  [][]int  `json:"groups"`  // partition: 分区分组
	From    int      `json:"from"`    // link
	To      int      `json:"to"`      // link
	Loss    *float64 `json:"loss"`    // link: 覆盖丢包率；nil 表示不修改
	Dup     *float64 `json:"dup"`     // link: 覆盖重复率
	Block   *bool    `json:"block"`   // link: 是否完全断开
}

// NodeRef 支持在脚本里写节点号 (1/2/3) 或 "leader"（当前领导者）。
type NodeRef struct {
	ID     int
	Leader bool
}

func (r *NodeRef) UnmarshalJSON(data []byte) error {
	var s string
	if err := json.Unmarshal(data, &s); err == nil {
		if s == "leader" {
			r.Leader = true
			return nil
		}
		id, err := strconv.Atoi(s)
		if err != nil {
			return fmt.Errorf("node ref must be 1..3 or \"leader\", got %q", s)
		}
		r.ID = id
		return nil
	}
	var n int
	if err := json.Unmarshal(data, &n); err != nil {
		return fmt.Errorf("node ref must be 1..3 or \"leader\", got %s", string(data))
	}
	r.ID = n
	return nil
}

func (s *Scenario) validate() error {
	if s.NodeCount != 3 {
		return fmt.Errorf("nodeCount must be 3 (fixed membership), got %d", s.NodeCount)
	}
	if s.EndTime <= 0 {
		return fmt.Errorf("endTime must be positive")
	}
	if s.Heartbeat <= 0 {
		return fmt.Errorf("heartbeat must be positive")
	}
	if s.Election.Min <= 0 || s.Election.Max < s.Election.Min {
		return fmt.Errorf("invalid electionTimeout: %+v", s.Election)
	}
	if s.Election.Min <= s.Heartbeat {
		return fmt.Errorf("electionTimeout.min (%d) must be greater than heartbeat (%d)",
			s.Election.Min, s.Heartbeat)
	}
	for id, nc := range s.Nodes {
		if id < 1 || id > s.NodeCount {
			return fmt.Errorf("node override for unknown node %d", id)
		}
		minV, maxV := s.Election.Min, s.Election.Max
		if nc.ElectionMin > 0 {
			minV = nc.ElectionMin
		}
		if nc.ElectionMax > 0 {
			maxV = nc.ElectionMax
		}
		if minV <= 0 || maxV < minV || minV <= s.Heartbeat {
			return fmt.Errorf("invalid election timeout override for node %d: [%d,%d]", id, minV, maxV)
		}
	}
	n := s.Network
	if n.LossPct < 0 || n.LossPct > 1 || n.DupPct < 0 || n.DupPct > 1 {
		return fmt.Errorf("network loss/dup probabilities must be within [0,1]")
	}
	if n.JitterPct < 0 || n.JitterPct > 1 {
		return fmt.Errorf("network jitterPct must be within [0,1]")
	}
	valid := map[string]bool{
		"client": true, "crash": true, "restart": true,
		"partition": true, "heal": true, "link": true,
	}
	for i, e := range s.Events {
		if !valid[e.Type] {
			return fmt.Errorf("events[%d]: unknown type %q", i, e.Type)
		}
		if e.Time < 0 || e.Time > s.EndTime {
			return fmt.Errorf("events[%d]: time %d out of range [0,%d]", i, e.Time, s.EndTime)
		}
		switch e.Type {
		case "client":
			if e.Node == nil {
				return fmt.Errorf("events[%d]: client event needs node", i)
			}
			if e.Client == "" {
				return fmt.Errorf("events[%d]: client event needs client id", i)
			}
		case "crash", "restart":
			if e.Node == nil {
				return fmt.Errorf("events[%d]: %s event needs node", i, e.Type)
			}
		case "partition":
			if len(e.Groups) == 0 {
				return fmt.Errorf("events[%d]: partition event needs groups", i)
			}
		case "link":
			if e.From == 0 || e.To == 0 {
				return fmt.Errorf("events[%d]: link event needs from/to", i)
			}
		}
	}
	return nil
}
