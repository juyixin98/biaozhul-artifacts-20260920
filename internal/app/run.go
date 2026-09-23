package app

import (
	"sort"

	"simsnap/internal/bank"
	"simsnap/internal/msg"
	"simsnap/internal/sim"
	"simsnap/internal/snap"
)

// EventKind 枚举输出日志中的事件类型。
const (
	EvScheduleTransfer = "schedule_transfer"
	EvInsufficient     = "insufficient"
	EvSend             = "send"
	EvDeliver          = "deliver"
	EvMarkerSend       = "marker_send"
	EvMarkerDeliver    = "marker_deliver"
	EvCaptured         = "snap_captured"
	EvDropDup          = "drop_dup"
	EvInitiate         = "snap_initiate"
	EvComplete         = "snap_complete"
)

// LogEvent 是时间线日志条目。
type LogEvent struct {
	At       int64  `json:"at"`
	Kind     string `json:"kind"`
	SnapID   int    `json:"snap_id,omitempty"`
	From     int    `json:"from,omitempty"`
	To       int    `json:"to,omitempty"`
	Amount   int    `json:"amount,omitempty"`
	TxnID    int64  `json:"txn_id,omitempty"`
	SendID   int    `json:"send_id,omitempty"`
	Dup      bool   `json:"dup,omitempty"`
	Note     string `json:"note,omitempty"`
	Balances []int  `json:"balances,omitempty"` // 仅 snap_complete：完成时刻各节点余额
}

// SnapshotResult 是单次快照的结果。
type SnapshotResult struct {
	Snapshot  snap.Snapshot `json:"snapshot"`
	NodeSum   int           `json:"node_sum"`  // 记录状态中余额之和
	InFlight  int           `json:"in_flight"` // 在途转账金额之和
	Total     int           `json:"total"`     // node_sum + in_flight
	Expected  int           `json:"expected"`  // 初始总余额（守恒参照）
	Conserved bool          `json:"conserved"` // complete && total == expected
}

// BalanceEntry 是结束时某节点余额。
type BalanceEntry struct {
	Node    int `json:"node"`
	Balance int `json:"balance"`
}

// LinkStat 是一条有向信道的结束统计。
type LinkStat struct {
	From  int       `json:"from"`
	To    int       `json:"to"`
	Stats sim.Stats `json:"stats"`
}

// Response 是一次模拟运行的输出。
type Response struct {
	Seed            int64            `json:"seed"`
	Now             int64            `json:"now"`
	Deadline        int64            `json:"deadline"`
	InitialTotal    int              `json:"initial_total"`
	FinalBalances   []BalanceEntry   `json:"final_balances"`
	FinalTotal      int              `json:"final_total"`
	Snapshots       []SnapshotResult `json:"snapshots"`
	Network         []LinkStat       `json:"network"`
	Events          []LogEvent       `json:"events"`
	StoppedDeadline bool             `json:"stopped_by_deadline,omitempty"`
}

// Run 执行请求并返回结果。请求必须已通过 Validate。
func Run(r *Request) *Response {
	n := len(r.Balances)
	eng := sim.NewEngine(r.Seed)
	if r.Deadline > 0 {
		eng.SetDeadline(r.Deadline)
	}
	netw := sim.NewNetwork(eng)
	policies, _ := r.Validate()
	for a := 0; a < n; a++ {
		for b := a + 1; b < n; b++ {
			netw.AddLink(a, b, policies[pairKey(a, b)])
		}
	}

	events := []LogEvent{}
	logf := func(e LogEvent) { events = append(events, e) }

	nodes := make([]*bank.Node, n)
	var mgr *snap.Manager
	logComplete := func(id int, at int64) {
		if mgr == nil || !mgr.IsComplete(id) {
			return
		}
		for _, e := range events {
			if e.Kind == EvComplete && e.SnapID == id {
				return
			}
		}
		bs := make([]int, n)
		for i, nd := range nodes {
			bs[i] = nd.Balance()
		}
		logf(LogEvent{At: at, Kind: EvComplete, SnapID: id, Balances: bs})
	}

	var send func(m msg.Message)
	send = func(m msg.Message) {
		isMarker := m.IsMarker()
		if !isMarker {
			mgr.NoteSend(m) // 先占位（丢包也要记录发送事实）
		}
		sent := netw.Send(m, func(d sim.Delivery) {
			mm := d.Msg
			if mm.IsMarker() {
				logf(LogEvent{At: d.At, Kind: EvMarkerDeliver, SnapID: mm.SnapID,
					From: mm.From, To: mm.To, SendID: d.SendID, Dup: d.Dup})
				if !d.Dup {
					mgr.MarkerReceived(mm.SnapID, mm.From, mm.To, d.At)
				}
				logComplete(mm.SnapID, d.At)
				return
			}
			logf(LogEvent{At: d.At, Kind: EvDeliver, From: mm.From, To: mm.To,
				Amount: mm.Amount, TxnID: mm.TxnID, SendID: d.SendID, Dup: d.Dup})
			mgr.NoteDelivery(mm, d.At)
			// Chandy–Lamport：先判定在途归属，再执行业务状态变更。
			captured := mgr.DataReceived(mm.From, mm.To, mm)
			for _, sid := range captured {
				logf(LogEvent{At: d.At, Kind: EvCaptured, SnapID: sid, From: mm.From, To: mm.To,
					Amount: mm.Amount, TxnID: mm.TxnID})
			}
			nodes[mm.To].Receive(mm.From, mm.Amount, mm.TxnID, d.At, d.Dup)
		})
		if !isMarker {
			mgr.SetSentAt(sent.From, sent.TxnID, sent.SentAt)
		}
		kind := EvSend
		if isMarker {
			kind = EvMarkerSend
		}
		logf(LogEvent{At: eng.Now(), Kind: kind, SnapID: sent.SnapID, From: sent.From, To: sent.To,
			Amount: sent.Amount, TxnID: sent.TxnID})
	}
	for i := 0; i < n; i++ {
		i := i
		nodes[i] = bank.New(i, r.Balances[i],
			func(to, amount int, txn int64) {
				send(msg.Message{Kind: msg.Data, From: i, To: to, Amount: amount, TxnID: txn})
			},
			func(ev bank.Event) {
				switch ev.Kind {
				case "insufficient":
					logf(LogEvent{At: ev.At, Kind: EvInsufficient, From: ev.Node, To: ev.Peer,
						Amount: ev.Amount, Note: ev.Note})
				case "drop_dup":
					logf(LogEvent{At: ev.At, Kind: EvDropDup, From: ev.Node, To: ev.Peer,
						Amount: ev.Amount, TxnID: ev.TxnID, Note: ev.Note})
				}
			})
	}

	mgr = snap.NewManager(n,
		func(from, to, snapID int) {
			send(msg.Message{Kind: msg.Marker, SnapID: snapID, From: from, To: to})
		},
		func(node int) any { return nodes[node].SnapshotState() },
	)

	initialTotal := 0
	for _, b := range r.Balances {
		initialTotal += b
	}

	// 脚本化事件：先排序保证调度顺序确定（同刻按发起方排序）。
	transfers := append([]TransferEvent(nil), r.Transfers...)
	sort.SliceStable(transfers, func(i, j int) bool {
		if transfers[i].At != transfers[j].At {
			return transfers[i].At < transfers[j].At
		}
		return transfers[i].From < transfers[j].From
	})
	for _, t := range transfers {
		t := t
		eng.ScheduleAt(t.At, 0, func(now int64) {
			logf(LogEvent{At: now, Kind: EvScheduleTransfer, From: t.From, To: t.To, Amount: t.Amount})
			nodes[t.From].Transfer(t.To, t.Amount, now)
		})
	}
	snaps := append([]SnapshotEvent(nil), r.Snapshots...)
	sort.SliceStable(snaps, func(i, j int) bool {
		if snaps[i].At != snaps[j].At {
			return snaps[i].At < snaps[j].At
		}
		return snaps[i].ID < snaps[j].ID
	})
	for _, s := range snaps {
		s := s
		eng.ScheduleAt(s.At, 0, func(now int64) {
			if mgr.Initiate(s.ID, s.Initiator, now) {
				logf(LogEvent{At: now, Kind: EvInitiate, SnapID: s.ID,
					From: s.Initiator, Note: "快照发起"})
			}
		})
	}

	// 持续随机流量：每个 tick 抽源/目的/金额，用的是引擎的可复现 RNG。
	if r.Traffic != nil {
		tc := r.Traffic
		var scheduleTick func(at int64)
		scheduleTick = func(at int64) {
			if at > tc.Until {
				return
			}
			eng.ScheduleAt(at, 0, func(now int64) {
				from := int(eng.RNG().Int63n(int64(n)))
				to := int(eng.RNG().Int63n(int64(n - 1)))
				if to >= from {
					to++
				}
				amount := int(eng.RNG().Int63n(int64(tc.MaxAmount))) + 1
				logf(LogEvent{At: now, Kind: EvScheduleTransfer, From: from, To: to, Amount: amount})
				nodes[from].Transfer(to, amount, now) // 余额不足时节点自身跳过
				scheduleTick(now + tc.Every)
			})
		}
		scheduleTick(tc.From)
	}

	eng.Run()

	// 汇总快照结果。
	allSnapIDs := map[int]bool{}
	for _, s := range r.Snapshots {
		allSnapIDs[s.ID] = true
	}
	for _, id := range mgr.CompletedIDs() {
		allSnapIDs[id] = true
	}
	ids := make([]int, 0, len(allSnapIDs))
	for id := range allSnapIDs {
		ids = append(ids, id)
	}
	sort.Ints(ids)
	resp := &Response{
		Seed:         r.Seed,
		Now:          eng.Now(),
		Deadline:     r.Deadline,
		InitialTotal: initialTotal,
		Events:       events,
	}
	for _, id := range ids {
		s := mgr.Build(id)
		res := SnapshotResult{Snapshot: s, Expected: initialTotal}
		for _, ns := range s.Nodes {
			if st, ok := ns.State.(*bank.State); ok {
				res.NodeSum += st.Balance + ns.OutAfter
			}
		}
		// 在途转账按唯一事务计金额：去重键为 (from,txn)，网络重复副本是同一笔钱。
		seenTxn := map[[2]int64]bool{}
		for _, ch := range s.Channels {
			for _, mm := range ch.Msgs {
				key := [2]int64{int64(mm.From), mm.TxnID}
				if !seenTxn[key] {
					seenTxn[key] = true
					res.InFlight += mm.Amount
				}
			}
		}
		res.Total = res.NodeSum + res.InFlight
		res.Conserved = s.Complete && res.Total == initialTotal
		resp.Snapshots = append(resp.Snapshots, res)
	}

	finalTotal := 0
	for i, nd := range nodes {
		resp.FinalBalances = append(resp.FinalBalances, BalanceEntry{Node: i, Balance: nd.Balance()})
		finalTotal += nd.Balance()
	}
	resp.FinalTotal = finalTotal

	for a := 0; a < n; a++ {
		for b := 0; b < n; b++ {
			if a == b {
				continue
			}
			resp.Network = append(resp.Network, LinkStat{From: a, To: b, Stats: netw.Stats(a, b)})
		}
	}
	if r.Deadline > 0 && eng.Pending() > 0 {
		resp.StoppedDeadline = true
	}
	return resp
}
