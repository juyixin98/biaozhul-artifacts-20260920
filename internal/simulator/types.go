package simulator

// Request 是一次确定性模拟运行的完整输入（来自 JSON）。
type Request struct {
	Comment   string     `json:"_comment,omitempty"` // 仅用于样例文件内的人读说明，模拟时忽略
	Seed      int64      `json:"seed"`
	TickLimit int        `json:"tick_limit,omitempty"` // 缺省：最后一个事件 tick + 100
	Nodes     []NodeSpec `json:"nodes"`
	Links     []LinkSpec `json:"links"`
	Events    []Event    `json:"events"`
}

type NodeSpec struct {
	Name    string `json:"name"`
	Balance int64  `json:"balance"`
}

// LinkSpec 描述一条有向链路 A -> B。
// Policy 为 "fifo"（缺省，可靠 FIFO）或 "best_effort"（可丢包/重复/乱序）。
type LinkSpec struct {
	From    string  `json:"from"`
	To      string  `json:"to"`
	Policy  string  `json:"policy,omitempty"`
	Base    int     `json:"base_delay,omitempty"` // 投递基准延迟，缺省 1
	Jitter  int     `json:"jitter,omitempty"`     // 额外随机延迟 [0, jitter]
	LossPct float64 `json:"loss_pct,omitempty"`   // 丢弃概率百分比 [0,100]
	DupPct  float64 `json:"dup_pct,omitempty"`    // 重复概率百分比 [0,100]
	Reorder bool    `json:"reorder,omitempty"`    // 是否允许乱序（提前到达）
}

// Event 是脚本事件。
//   - kind "transfer": from -> to 的一笔转账，amount > 0
//   - kind "marker":   from 发起 Chandy-Lamport 快照 snapshot_id
//
// 脚本事件本身按出现顺序在同一 tick 内依次处理；repeat_to > tick 时周期性重复。
type Event struct {
	Kind        string `json:"kind"`
	Tick        int    `json:"tick"`
	From        string `json:"from,omitempty"`
	To          string `json:"to,omitempty"`
	Amount      int64  `json:"amount,omitempty"`
	SnapshotID  string `json:"snapshot_id,omitempty"`
	RepeatTo    int    `json:"repeat_to,omitempty"`
	RepeatEvery int    `json:"repeat_every,omitempty"` // 缺省 1
}

// Response 是一次运行的完整结果（输出为 JSON）。
type Response struct {
	Seed          int64            `json:"seed"`
	LastTick      int              `json:"last_tick"`
	FinalBalances map[string]int64 `json:"final_balances"`
	Snapshots     []SnapshotResult `json:"snapshots"`
	ChannelStats  []ChannelStat    `json:"channel_stats"`
	Log           []LogEntry       `json:"log"`
}

type SnapshotResult struct {
	ID          string           `json:"id"`
	Complete    bool             `json:"complete"`
	Initiator   string           `json:"initiator"`
	States      map[string]int64 `json:"states"`
	InFlight    []ChannelSnap    `json:"in_flight"`
	StatesSum   int64            `json:"states_sum"`
	InFlightSum int64            `json:"in_flight_sum"`
	Total       int64            `json:"total"`
	Missing     []string         `json:"missing_channels,omitempty"`
}

type ChannelSnap struct {
	From    string  `json:"from"`
	To      string  `json:"to"`
	Count   int     `json:"count"`
	Sum     int64   `json:"sum"`
	Amounts []int64 `json:"amounts"`
}

type ChannelStat struct {
	From       string `json:"from"`
	To         string `json:"to"`
	Policy     string `json:"policy"`
	Sent       int    `json:"sent"`
	Delivered  int    `json:"delivered"`
	Lost       int    `json:"lost"`
	Duplicated int    `json:"duplicated"`
	Reordered  int    `json:"reordered"`
}

type LogEntry struct {
	Tick   int    `json:"tick"`
	Kind   string `json:"kind"` // send | deliver | marker | snapshot
	From   string `json:"from,omitempty"`
	To     string `json:"to,omitempty"`
	Amount int64  `json:"amount,omitempty"`
	SnapID string `json:"snapshot_id,omitempty"`
	Detail string `json:"detail,omitempty"`
}
