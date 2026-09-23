package sim

import (
	"fmt"
	"math/rand"
	"path/filepath"

	"raftrun/internal/raft"
)

// linkState 是一条有向链路的当前参数。
type linkState struct {
	loss    float64
	dup     float64
	blocked bool
}

// nodeRuntime 封装一个 Raft 节点及其模拟器侧状态。
type nodeRuntime struct {
	id      int
	node    *raft.Node
	store   raft.Storage
	rng     *rand.Rand
	alive   bool
	epoch   int64 // 每次（重新）启动加一，用于使旧定时器失效
	dataDir string
}

// TraceEvent 是运行轨迹中的一条记录（可选输出）。
type TraceEvent struct {
	Time   int64       `json:"time"`
	Kind   string      `json:"kind"`
	From   int         `json:"from,omitempty"`
	To     int         `json:"to,omitempty"`
	Term   int         `json:"term,omitempty"`
	Detail string      `json:"detail,omitempty"`
	Msg    interface{} `json:"msg,omitempty"`
}

// ProposalResult 是单个 client 写入的最终归宿。
type ProposalResult struct {
	Client  string `json:"client"`
	Node    int    `json:"node"`
	Command string `json:"command"`
	Status  string `json:"status"` // accepted | notLeader | nodeDown
	Index   int    `json:"index,omitempty"`
	Term    int    `json:"term,omitempty"`
}

// CommittedEntry 是模拟结束观察到的一条已提交日志。
type CommittedEntry struct {
	Index   int    `json:"index"`
	Term    int    `json:"term"`
	Command string `json:"command"`
	Client  string `json:"client,omitempty"`
}

// Result 是一次模拟的完整输出。
type Result struct {
	Name       string           `json:"name"`
	EndTime    int64            `json:"endTime"`
	AliveNodes map[int]bool     `json:"aliveNodes"`
	Proposals  []ProposalResult `json:"proposals"`
	// Committed 是任意存活节点观察到的最长已提交前缀（各节点应一致）。
	Committed []CommittedEntry `json:"committed"`
	// NodeCommitIndex 为每个存活节点最终的 commitIndex。
	NodeCommitIndex map[int]int `json:"nodeCommitIndex"`
	// NodeLogLength 为每个存活节点最终的日志长度。
	NodeLogLength  map[int]int    `json:"nodeLogLength"`
	LeaderChanges  []LeaderChange `json:"leaderChanges"`
	InvariantCheck InvariantCheck `json:"invariantCheck"`
	Trace          []TraceEvent   `json:"trace,omitempty"`
}

// LeaderChange 记录一次领导者观测变化。
type LeaderChange struct {
	Time int64 `json:"time"`
	Term int   `json:"term"`
	Node int   `json:"node"`
}

// InvariantCheck 是安全性断言结果。
type InvariantCheck struct {
	Passed bool     `json:"passed"`
	Errors []string `json:"errors"`
}

// Simulator 是一次模拟运行。
type Simulator struct {
	sc   *Scenario
	rng  *rand.Rand
	q    *eventQueue
	time int64

	nodes map[int]*nodeRuntime
	links map[[2]int]*linkState // 有向链路 [from,to]

	proposals []ProposalResult
	trace     []TraceEvent

	// globalCommitted: 从各节点已提交前缀观察到的索引 -> 条目。
	globalCommitted map[int]raft.Entry
	leaderChanges   []LeaderChange
	lastLeader      map[int]int // 节点ID -> 上次观测到它自认为的领导者
}

// Run 读取、校验场景并执行一次确定性模拟，返回 JSON 可序列化结果。
func Run(sc *Scenario) (*Result, error) {
	if err := sc.validate(); err != nil {
		return nil, fmt.Errorf("invalid scenario: %w", err)
	}
	s := &Simulator{
		sc:              sc,
		rng:             rand.New(rand.NewSource(sc.Seed)),
		q:               newEventQueue(),
		nodes:           make(map[int]*nodeRuntime),
		links:           make(map[[2]int]*linkState),
		globalCommitted: make(map[int]raft.Entry),
		lastLeader:      make(map[int]int),
	}

	// 初始化双向链路默认参数。
	for a := 1; a <= 3; a++ {
		for b := 1; b <= 3; b++ {
			if a == b {
				continue
			}
			s.links[[2]int{a, b}] = &linkState{
				loss: sc.Network.LossPct,
				dup:  sc.Network.DupPct,
			}
		}
	}

	// 启动全部节点（清除旧状态文件，保证全新集群）。
	for id := 1; id <= 3; id++ {
		dir := filepath.Join(sc.DataDir, fmt.Sprintf("node%d", id))
		store, err := raft.NewFileStorage(dir)
		if err != nil {
			return nil, err
		}
		if err := store.Reset(); err != nil {
			return nil, err
		}
		s.startNode(id, store)
	}

	// 外部事件按时间入队（同刻保持脚本顺序）。
	for i := range sc.Events {
		s.q.push(&event{time: sc.Events[i].Time, kind: evExternal,
			msg: &delayedMsg{payload: &sc.Events[i]}})
	}

	s.mainLoop()
	return s.buildResult(), nil
}

// startNode 以给定存储创建并启动一个节点（首次启动或崩溃后重启）。
func (s *Simulator) startNode(id int, store raft.Storage) {
	peers := []int{1, 2, 3}
	cfg := raft.Config{
		ID:              id,
		Peers:           peers,
		HeartbeatPeriod: s.sc.Heartbeat,
		ElectionMin:     s.sc.Election.Min,
		ElectionMax:     s.sc.Election.Max,
	}
	if nc, ok := s.sc.Nodes[id]; ok {
		if nc.ElectionMin > 0 {
			cfg.ElectionMin = nc.ElectionMin
		}
		if nc.ElectionMax > 0 {
			cfg.ElectionMax = nc.ElectionMax
		}
	}
	nr := &nodeRuntime{
		id:      id,
		store:   store,
		rng:     rand.New(rand.NewSource(s.rng.Int63())),
		alive:   true,
		dataDir: filepath.Join(s.sc.DataDir, fmt.Sprintf("node%d", id)),
	}
	if old := s.nodes[id]; old != nil {
		nr.epoch = old.epoch + 1
	}
	env := &nodeEnv{s: s, nr: nr}
	nr.node = raft.NewNode(cfg, env, store, nr.rng)
	s.nodes[id] = nr
}
