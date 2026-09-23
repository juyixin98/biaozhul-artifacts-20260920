package runner

import (
	"encoding/json"
	"sort"
	"strconv"

	"twopcsim/internal/twopc"
	"twopcsim/internal/wal"
)

// TxnOutcome 是单个事务的最终裁决与证据。
type TxnOutcome struct {
	TxnID           string           `json:"txnId"`
	Verdict         string           `json:"verdict"` // committed | aborted | blocked | partial-commit
	Committed       []string         `json:"committed"`
	Aborted         []string         `json:"aborted"`
	Prepared        []string         `json:"prepared"` // 窗口结束仍 prepared（持锁）的参与者
	Unknown         []string         `json:"unknown"`
	PartialCommit   bool             `json:"partialCommit"` // true=出现部分参与者提交（协议被破坏）
	BlockedEvidence *BlockedEvidence `json:"blockedEvidence,omitempty"`
}

// BlockedEvidence 展示“真的阻塞了”，而不是伪称可用。
type BlockedEvidence struct {
	CoordinatorAvailable bool                `json:"coordinatorAvailable"`
	Participants         []BlockedPart       `json:"participants"`
	CoordinatorDecision  string              `json:"coordinatorDecision"` // unknown | commit | abort
	QueryTicks           []QueryTickEvidence `json:"queryTicks,omitempty"`
	Explanation          string              `json:"explanation"`
}

// BlockedPart 是单个被阻塞参与者的持锁证据。
type BlockedPart struct {
	NodeID        string `json:"nodeId"`
	State         string `json:"state"`
	LockHeld      bool   `json:"lockHeld"`
	PreparedTick  int64  `json:"preparedTick"`
	WaitedTicks   int64  `json:"waitedTicks"`
	LastQueryTick int64  `json:"lastQueryTick"`
	QueriesSent   int    `json:"queriesSent"`
}

// QueryTickEvidence 记录一次询问周期的时刻。
type QueryTickEvidence struct {
	Tick int64  `json:"tick"`
	From string `json:"from"`
}

// Report 是一次运行的完整 JSON 输出。
type Report struct {
	Scenario           string               `json:"scenario"`
	Seed               int64                `json:"seed"`
	MaxTick            int64                `json:"maxTick"`
	DataDir            string               `json:"dataDir"`
	Network            NetworkCfg           `json:"network"`
	Timings            twopc.Timings        `json:"timings"`
	NodeSnapshots      []twopc.NodeSnapshot `json:"nodeSnapshots"`
	Outcomes           []TxnOutcome         `json:"outcomes"`
	Verdict            string               `json:"verdict"` // all-consistent | partial-commit-detected
	PartialCommits     []string             `json:"partialCommits,omitempty"`
	ProtocolViolations []TraceEntry         `json:"protocolViolations,omitempty"`
	Crashes            []CrashSummary       `json:"crashes"`
	Trace              []TraceEntry         `json:"trace"`
}

// CrashSummary 汇总崩溃/重启是否按计划发生（验证注入生效）。
type CrashSummary struct {
	NodeID      string `json:"nodeId"`
	Trigger     string `json:"trigger"`
	Crashed     bool   `json:"crashed"`
	RestartTick int64  `json:"restartTick,omitempty"`
	Restarted   bool   `json:"restarted"`
}

// Report 生成最终报告。
func (r *Runner) Report() *Report {
	// 1) 节点快照：存活用内存视图；宕机用磁盘 WAL 重放重建。
	nodeIDs := append([]string{CoordinatorID}, ParticipantIDs()...)
	snaps := make([]twopc.NodeSnapshot, 0, len(nodeIDs))
	nodeStates := map[string]map[string]string{}
	for _, id := range nodeIDs {
		var snap twopc.NodeSnapshot
		if r.down[id] {
			snap = r.snapshotFromWAL(id)
		} else {
			snap = r.nodes[id].Snapshot()
		}
		snaps = append(snaps, snap)
		states := map[string]string{}
		for txn, v := range snap.TxnStates {
			states[txn] = v.State
		}
		nodeStates[id] = states
	}

	// 2) 逐事务裁决：本轮 Begin 的事务 + 磁盘 WAL 中存在的历史事务（resume）。
	txnIDs := []string{}
	seen := map[string]bool{}
	for _, t := range r.sc.Transactions {
		if !seen[t.ID] {
			seen[t.ID] = true
			txnIDs = append(txnIDs, t.ID)
		}
	}
	for _, snap := range snaps {
		if snap.Role != "coordinator" {
			continue
		}
		ids := make([]string, 0, len(snap.TxnStates))
		for id := range snap.TxnStates {
			ids = append(ids, id)
		}
		sort.Strings(ids)
		for _, id := range ids {
			if !seen[id] {
				seen[id] = true
				txnIDs = append(txnIDs, id)
			}
		}
	}

	outcomes := make([]TxnOutcome, 0, len(txnIDs))
	var partials []string
	for _, txnID := range txnIDs {
		o := r.judgeTxn(txnID, nodeStates)
		if o.PartialCommit {
			partials = append(partials, txnID)
		}
		outcomes = append(outcomes, o)
	}

	// 3) 协议违规事件。
	var violations []TraceEntry
	for _, e := range r.trace {
		if e.Kind == "participant.protocolViolation" {
			violations = append(violations, e)
		}
	}

	verdict := "all-consistent"
	if len(partials) > 0 {
		verdict = "partial-commit-detected"
	}

	return &Report{
		Scenario:           r.sc.Name,
		Seed:               r.sc.Seed,
		MaxTick:            r.sc.MaxTick,
		DataDir:            r.sc.DataDir,
		Network:            r.sc.Network,
		Timings:            r.sc.Timings,
		NodeSnapshots:      snaps,
		Outcomes:           outcomes,
		Verdict:            verdict,
		PartialCommits:     partials,
		ProtocolViolations: violations,
		Crashes:            r.crashSummary(),
		Trace:              r.trace,
	}
}

func (r *Runner) judgeTxn(txnID string, nodeStates map[string]map[string]string) TxnOutcome {
	o := TxnOutcome{TxnID: txnID}
	parts := append([]string{}, ParticipantIDs()...)
	sort.Strings(parts)
	for _, p := range parts {
		st := nodeStates[p][txnID]
		if st == "" {
			st = twopc.StateUnknown
		}
		switch st {
		case twopc.StateCommitted:
			o.Committed = append(o.Committed, p)
		case twopc.StateAborted:
			o.Aborted = append(o.Aborted, p)
		case twopc.StatePrepared:
			o.Prepared = append(o.Prepared, p)
		default:
			o.Unknown = append(o.Unknown, p)
		}
	}

	// 无部分提交判定：
	// committed 与 (aborted|unknown) 并存 => 不可调和的分叉。
	// prepared 不算分叉：决议提交时它只是还没收到第二阶段，恢复后可收敛。
	if len(o.Committed) > 0 && (len(o.Aborted) > 0 || len(o.Unknown) > 0) {
		o.PartialCommit = true
		o.Verdict = "partial-commit"
	} else if len(o.Committed) == 3 {
		o.Verdict = twopc.StateCommitted
	} else if len(o.Committed) > 0 && len(o.Prepared) > 0 {
		// 决议已提交，部分参与者尚停在 prepared（协调者此刻可能又宕机）=> 阻塞。
		o.Verdict = "blocked"
	} else if len(o.Prepared) > 0 {
		o.Verdict = "blocked"
	} else {
		o.Verdict = twopc.StateAborted
	}

	if o.Verdict == "blocked" {
		o.BlockedEvidence = r.blockedEvidence(txnID, nodeStates)
	}
	return o
}

func (r *Runner) blockedEvidence(txnID string, nodeStates map[string]map[string]string) *BlockedEvidence {
	ev := &BlockedEvidence{CoordinatorAvailable: !r.down[CoordinatorID]}
	switch nodeStates[CoordinatorID][txnID] {
	case twopc.StateCommitting:
		ev.CoordinatorDecision = "commit"
	case twopc.StateAborting:
		ev.CoordinatorDecision = "abort"
	default:
		ev.CoordinatorDecision = "unknown"
	}

	// 首次 PREPARED 时刻、询问次数（全部来自追踪事件）。
	prepTick := map[string]int64{}
	lastQuery := map[string]int64{}
	queries := map[string]int{}
	for _, e := range r.trace {
		if e.TxnID != txnID {
			continue
		}
		if e.Kind == "participant.prepared.fsynced" {
			if _, ok := prepTick[e.Node]; !ok {
				prepTick[e.Node] = e.Tick
			}
		}
		if e.Kind == "participant.query.tick" {
			lastQuery[e.Node] = e.Tick
			queries[e.Node]++
			if len(ev.QueryTicks) < 30 {
				ev.QueryTicks = append(ev.QueryTicks, QueryTickEvidence{Tick: e.Tick, From: e.Node})
			}
		}
	}

	ids := append([]string{}, ParticipantIDs()...)
	sort.Strings(ids)
	for _, p := range ids {
		if nodeStates[p][txnID] != twopc.StatePrepared {
			continue
		}
		pt := prepTick[p]
		ev.Participants = append(ev.Participants, BlockedPart{
			NodeID:        p,
			State:         twopc.StatePrepared,
			LockHeld:      true,
			PreparedTick:  pt,
			WaitedTicks:   r.sc.MaxTick - pt,
			LastQueryTick: lastQuery[p],
			QueriesSent:   queries[p],
		})
	}

	switch {
	case !ev.CoordinatorAvailable && ev.CoordinatorDecision == "unknown":
		ev.Explanation = "协调者在形成决议前不可用：已投赞成票的参与者持有 PREPARED 记录与资源锁，" +
			"既不能提交也不敢中止（同伴同样无决议），只能周期性询问并阻塞。这是 2PC 的固有阻塞点，" +
			"本模拟器如实呈现，不假设协调者始终可用。"
	case !ev.CoordinatorAvailable && ev.CoordinatorDecision == "commit":
		ev.Explanation = "协调者在 fsync COMMIT 后、通知完成前不可用：决议本身已是提交且不可撤销，" +
			"未收到 GLOBAL_COMMIT 的参与者持锁阻塞，直到协调者（或已提交同伴）恢复告知决议。"
	case !ev.CoordinatorAvailable && ev.CoordinatorDecision == "abort":
		ev.Explanation = "协调者已 fsync ABORT 但在通知前不可用：PREPARED 参与者持锁等待中止决议送达。"
	default:
		ev.Explanation = "参与者处于 PREPARED 且在整个观察窗口内未获得任何权威决议；持锁等待中（阻塞）。"
	}
	return ev
}

// snapshotFromWAL 重放宕机节点的磁盘日志，重建其持久状态（无易失状态）。
func (r *Runner) snapshotFromWAL(id string) twopc.NodeSnapshot {
	snap := twopc.NodeSnapshot{ID: id, TxnStates: map[string]twopc.TxnView{}}
	_ = r.walh[id].Replay(func(rec wal.Record) error {
		var d struct {
			TxnID string `json:"txnId"`
		}
		if err := json.Unmarshal(rec.Data, &d); err != nil {
			return err
		}
		switch rec.Type {
		case "COORD_START":
			if _, ok := snap.TxnStates[d.TxnID]; !ok {
				snap.TxnStates[d.TxnID] = twopc.TxnView{State: twopc.StatePreparing}
			}
		case "COORD_COMMIT":
			snap.TxnStates[d.TxnID] = twopc.TxnView{State: twopc.StateCommitting}
		case "COORD_ABORT":
			snap.TxnStates[d.TxnID] = twopc.TxnView{State: twopc.StateAborting}
		case "PART_COMMITTED":
			snap.TxnStates[d.TxnID] = twopc.TxnView{State: twopc.StateCommitted}
		case "PART_ABORTED":
			snap.TxnStates[d.TxnID] = twopc.TxnView{State: twopc.StateAborted}
		case "PART_PREPARED":
			// 终态记录优先（重放顺序保证终态在后，这里只在尚无记录时写入）。
			if _, ok := snap.TxnStates[d.TxnID]; !ok {
				snap.TxnStates[d.TxnID] = twopc.TxnView{State: twopc.StatePrepared, LockHeld: true}
			}
		}
		return nil
	})
	if id == CoordinatorID {
		snap.Role = "coordinator"
	} else {
		snap.Role = "participant"
	}
	return snap
}

func (r *Runner) crashSummary() []CrashSummary {
	out := []CrashSummary{}
	for _, cs := range r.sortedCrashStates() {
		trig := cs.rule.Hook
		if trig == "" {
			trig = "atTick:" + strconv.FormatInt(cs.rule.AtTick, 10)
		}
		out = append(out, CrashSummary{
			NodeID:      cs.rule.NodeID,
			Trigger:     trig,
			Crashed:     cs.crashed,
			RestartTick: cs.rule.RestartTick,
			Restarted:   cs.restarted,
		})
	}
	return out
}
