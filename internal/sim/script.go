// Package sim 是确定性离散事件模拟器：按固定 tick 驱动若干 Raft 节点，
// 按 JSON 脚本注入客户端写、崩溃/重启、网络分区与断言事件。
//
// 同一个脚本（相同种子）在任意机器上重复运行，结果必须逐字节一致。
package sim

import (
	"encoding/json"
	"fmt"
	"os"

	"raftdemo/internal/network"
)

// Script 是一次模拟运行的完整输入（JSON 请求）。
type Script struct {
	Seed        int64     `json:"seed"`
	Ticks       int       `json:"ticks"`        // 总运行 tick 数（含）
	Nodes       int       `json:"nodes"`        // 必须为 3（本项目固定）
	Heartbeat   int       `json:"heartbeat"`    // 心跳间隔（tick），默认 3
	ElectionLo  int       `json:"election_lo"`  // 选举超时下界，默认 8
	ElectionHi  int       `json:"election_hi"`  // 选举超时上界（不含），默认 16
	NodeWindows []Window  `json:"node_windows"` // 按节点覆盖选举窗口（用于编排确定性选举）
	Storage     string    `json:"storage"`      // "memory"（默认）或 "file"
	StateDir    string    `json:"state_dir"`    // file 存储的根目录（每节点一个子目录）
	FreshState  bool      `json:"fresh_state"`  // file 存储：启动前清空 state_dir，保证样例可重复运行
	Network     NetConfig `json:"network"`      // 全局网络默认故障参数
	Events      []Event   `json:"events"`
}

// Window 覆盖单个节点的选举超时区间。
type Window struct {
	Node int `json:"node"`
	Lo   int `json:"lo"`
	Hi   int `json:"hi"`
}

// NetConfig 是所有链路的初始故障参数。
type NetConfig struct {
	DelayTicks int     `json:"delay_ticks"` // 默认 1
	Jitter     int     `json:"jitter"`      // 投递时间随机抖动
	LossRate   float64 `json:"loss_rate"`
	DupRate    float64 `json:"dup_rate"`
}

// Event 是脚本事件。Kind 决定使用哪些字段，未使用字段必须省略。
type Event struct {
	At   int    `json:"at"`
	Kind string `json:"kind"`

	// client_write
	Client string `json:"client,omitempty"`
	Data   string `json:"data,omitempty"`
	Node   *int   `json:"node,omitempty"` // 不指定时发给当前领导者

	// crash / restart / wipe
	Target *int `json:"target,omitempty"`

	// partition
	Partition []int `json:"partition,omitempty"` // 长度=N 的分区号数组
	Heal      bool  `json:"heal,omitempty"`

	// isolate
	Isolated *int `json:"isolated,omitempty"`

	// link
	From *int          `json:"from,omitempty"`
	To   *int          `json:"to,omitempty"`
	Link *network.Link `json:"link,omitempty"` // null 表示恢复默认

	// network_reset：无额外字段，恢复全部链路为默认参数

	// 断言类：committed / not_acked / log_contains / log_match / commit_index
	ExpectIndex *int  `json:"expect_index,omitempty"`
	Committed   *bool `json:"committed,omitempty"` // committed: 要求已提交；not_acked: 要求未确认
	FullLog     *bool `json:"full_log,omitempty"`  // log_contains：true 检查完整日志而非仅已提交
	Quorum      *bool `json:"quorum,omitempty"`    // committed/not_acked：按多数派共同提交判定
}

// AssertionFailure 记录一条失败的断言或安全不变量。
type AssertionFailure struct {
	At      int    `json:"at"`
	Kind    string `json:"kind"`
	Message string `json:"message"`
}

// TraceEntry 是运行轨迹中的一条记录（精简字段，按类型取用）。
type TraceEntry struct {
	At     int    `json:"at"`
	Event  string `json:"event"`
	Node   int    `json:"node,omitempty"`
	Term   int    `json:"term,omitempty"`
	Role   string `json:"role,omitempty"`
	Data   string `json:"data,omitempty"`
	Client string `json:"client,omitempty"`
	Detail string `json:"detail,omitempty"`
}

// NodeResult 是运行结束时单个节点的状态快照。
type NodeResult struct {
	ID          int         `json:"id"`
	Alive       bool        `json:"alive"`
	Role        string      `json:"role"`
	Term        int         `json:"term"`
	CommitIndex int         `json:"commit_index"`
	Committed   []EntryView `json:"committed"`
	FullLog     []EntryView `json:"full_log"`
}

// EntryView 是日志条目的可读视图。
type EntryView struct {
	Index    int    `json:"index"`
	Term     int    `json:"term"`
	Data     string `json:"data"`
	ClientID string `json:"client_id,omitempty"`
}

// Result 是一次运行的输出（JSON 响应）。
type Result struct {
	OK          bool               `json:"ok"`
	TicksRun    int                `json:"ticks_run"`
	TermHistory []LeaderView       `json:"term_history"`
	Acks        []AckView          `json:"acks"`
	Nodes       []NodeResult       `json:"nodes"`
	Network     NetworkStats       `json:"network"`
	Failures    []AssertionFailure `json:"failures"`
	Trace       []TraceEntry       `json:"trace,omitempty"`
}

// LeaderView 记录每个出现过领导者的任期。
type LeaderView struct {
	Term   int `json:"term"`
	Leader int `json:"leader"`
}

// AckView 记录一次客户端写的确认。
type AckView struct {
	Client    string `json:"client"`
	Data      string `json:"data"`
	Leader    int    `json:"leader"`
	Term      int    `json:"term"`
	AppliedAt int    `json:"applied_at"`
	Index     int    `json:"index"`
}

// NetworkStats 是网络层统计。
type NetworkStats struct {
	Dropped    int `json:"dropped"`
	Duplicated int `json:"duplicated"`
}

// LoadScript 从 JSON 文件读取脚本。
func LoadScript(path string) (*Script, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return ParseScript(raw)
}

// ParseScript 解析并校验 JSON 脚本，填充默认值。
func ParseScript(raw []byte) (*Script, error) {
	var s Script
	if err := json.Unmarshal(raw, &s); err != nil {
		return nil, fmt.Errorf("脚本 JSON 解析失败: %w", err)
	}
	if err := s.fillDefaultsAndValidate(); err != nil {
		return nil, err
	}
	return &s, nil
}

func (s *Script) fillDefaultsAndValidate() error {
	if s.Nodes == 0 {
		s.Nodes = 3
	}
	if s.Nodes != 3 {
		return fmt.Errorf("本项目固定 3 节点，脚本 nodes=%d", s.Nodes)
	}
	if s.Ticks <= 0 {
		return fmt.Errorf("ticks 必须为正数")
	}
	if s.Heartbeat <= 0 {
		s.Heartbeat = 3
	}
	if s.ElectionLo <= 0 {
		s.ElectionLo = 8
	}
	if s.ElectionHi <= s.ElectionLo {
		s.ElectionHi = s.ElectionLo * 2
	}
	if s.Storage == "" {
		s.Storage = "memory"
	}
	if s.Storage != "memory" && s.Storage != "file" {
		return fmt.Errorf("storage 只能是 memory 或 file，得到 %q", s.Storage)
	}
	if s.Network.DelayTicks <= 0 {
		s.Network.DelayTicks = 1
	}
	for _, w := range s.NodeWindows {
		if w.Node < 0 || w.Node >= s.Nodes || w.Lo <= 0 || w.Hi <= w.Lo {
			return fmt.Errorf("非法 node_windows: %+v", w)
		}
	}
	validKinds := map[string]bool{
		"client_write": true, "crash": true, "restart": true, "wipe": true,
		"partition": true, "isolate": true, "link": true, "network_reset": true,
		"committed": true, "not_acked": true, "log_contains": true,
		"log_match": true, "commit_index": true,
	}
	for i, e := range s.Events {
		if e.At < 0 || e.At > s.Ticks {
			return fmt.Errorf("事件 %d 的 at 越界: %d", i, e.At)
		}
		if !validKinds[e.Kind] {
			return fmt.Errorf("事件 %d 的 kind 未知: %q", i, e.Kind)
		}
	}
	return nil
}
