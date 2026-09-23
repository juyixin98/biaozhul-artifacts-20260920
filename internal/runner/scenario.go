// Package runner 在单进程内把协调者、3 个参与者、有损网络、
// 崩溃/重启注入和确定性时钟组装成一次可复现的模拟运行。
package runner

import "twopcsim/internal/twopc"

// Scenario 是一次运行的完整 JSON 输入。
type Scenario struct {
	Name string `json:"name"`
	Seed int64  `json:"seed"`

	// 虚拟时钟观察窗口：超过 maxTick 仍未终结的参与者即判为阻塞。
	MaxTick int64 `json:"maxTick"`

	// 数据目录：各节点 WAL 写入 <dataDir>/<nodeID>.wal。
	// 留空则使用临时目录；resume 运行时显式传入上一次的目录。
	DataDir string `json:"dataDir,omitempty"`
	// Fresh 为 true 时先清空数据目录（默认 true；resume 时设为 false）。
	Fresh *bool `json:"fresh,omitempty"`

	Network NetworkCfg    `json:"network"`
	Timings twopc.Timings `json:"timings"`

	// Transactions 本轮 Begin 的事务。
	Transactions []TxnSpec `json:"transactions"`

	// Crashes 崩溃/重启注入规则。
	Crashes []CrashRule `json:"crashes,omitempty"`
}

// NetworkCfg 配置有损网络（确定性）。
type NetworkCfg struct {
	LossRate      float64 `json:"lossRate"`      // 每条消息独立丢弃概率
	DuplicateRate float64 `json:"duplicateRate"` // 每条消息额外复制一份的概率
	ReorderRate   float64 `json:"reorderRate"`   // 消息乱序（附加随机延迟）概率
	MinDelay      int64   `json:"minDelay"`      // 正常传播延迟下界
	MaxDelay      int64   `json:"maxDelay"`      // 正常传播延迟上界
	// ReorderDelay 为乱序消息追加的额外延迟。
	ReorderDelay int64 `json:"reorderDelay"`
}

// TxnSpec 描述一个事务。
type TxnSpec struct {
	ID        string   `json:"id"`
	BeginTick int64    `json:"beginTick"` // 在该 tick 由协调者 Begin
	VoteNo    []string `json:"voteNo"`    // 这些参与者本地投否决（可空）
}

// CrashRule 描述一次崩溃注入。
type CrashRule struct {
	NodeID string `json:"nodeId"`         // coordinator / participant-1..3
	Hook   string `json:"hook,omitempty"` // 协议钩子名（与 atTick 二选一）
	AtTick int64  `json:"atTick,omitempty"`
	// Occurrence 钩子第几次命中时崩溃（默认 1）。
	Occurrence int `json:"occurrence,omitempty"`
	// TxnID 仅对指定事务的钩子生效（可空=任意事务）。
	TxnID string `json:"txnId,omitempty"`
	// RestartTick 重启时刻；为 0 表示本轮永不重启（保持不可用）。
	RestartTick int64 `json:"restartTick,omitempty"`
}
