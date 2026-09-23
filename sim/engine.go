package sim

import (
	"container/heap"
	"sort"

	"orset/crdt"
)

// TraceEntry 记录模拟过程中的一个可观察动作，按发生顺序排列。
type TraceEntry struct {
	At         int      `json:"at"`
	Kind       string   `json:"kind"` // add | remove | send | deliver
	Replica    string   `json:"replica,omitempty"`
	Value      string   `json:"value,omitempty"`
	From       string   `json:"from,omitempty"`
	To         string   `json:"to,omitempty"`
	Tag        string   `json:"tag,omitempty"`
	Removed    []string `json:"removed_tags,omitempty"`
	SendSeq    int      `json:"send_seq,omitempty"`
	Delay      int      `json:"delay,omitempty"`
	Dropped    bool     `json:"dropped,omitempty"`
	Duplicated bool     `json:"duplicated,omitempty"`
	Reordered  bool     `json:"reordered,omitempty"`
	Redundant  bool     `json:"redundant,omitempty"` // 接收状态未变化（幂等/旧消息）
	Values     []string `json:"values,omitempty"`    // 动作后相关副本的有效元素
}

// Stats 汇总网络与合并计数。
type Stats struct {
	LocalAdds         int `json:"local_adds"`
	LocalRemoves      int `json:"local_removes"`
	MessagesSent      int `json:"messages_sent"`
	MessagesDropped   int `json:"messages_dropped"`
	DuplicatesMade    int `json:"duplicates_made"`
	MessagesDelivered int `json:"messages_delivered"`
	RedundantMerges   int `json:"redundant_merges"`
	Reorders          int `json:"reorders"` // 实际发生的“后发先至”次数
	ForcedReorders    int `json:"forced_reorders"`
	FinalTick         int `json:"final_tick"`
}

// NodeResult 是单个副本的终态。
type NodeResult struct {
	Values []string            `json:"values"`
	A      map[string][]string `json:"a"`
	R      map[string][]string `json:"r"`
}

// Result 是一次模拟的完整输出（JSON 运行接口的响应体）。
type Result struct {
	Name            string                `json:"name"`
	Seed            uint64                `json:"seed"`
	Replicas        []string              `json:"replicas"`
	FinalTick       int                   `json:"final_tick"`
	Converged       bool                  `json:"converged"`        // 全部副本 A/R 状态完全相同
	ValuesConverged bool                  `json:"values_converged"` // 仅有效元素相同
	ExpectedValues  []string              `json:"expected_values,omitempty"`
	MatchesExpected bool                  `json:"matches_expected"`
	ExpectedGiven   bool                  `json:"expected_given"`
	Stats           Stats                 `json:"stats"`
	Nodes           map[string]NodeResult `json:"nodes"`
	Trace           []TraceEntry          `json:"trace"`
}

// ---- 内部事件堆：严格 (at, seq) 序，保证确定性 ----

type timer struct {
	at  int
	seq int

	kind    string // local:add | local:remove | local:sync | local:gossip | deliver
	replica string
	value   string // add/remove: 元素；sync: 目标副本
	msg     *message
}

type eventHeap []*timer

func (h eventHeap) Len() int { return len(h) }
func (h eventHeap) Less(i, j int) bool {
	if h[i].at != h[j].at {
		return h[i].at < h[j].at
	}
	return h[i].seq < h[j].seq
}
func (h eventHeap) Swap(i, j int)       { h[i], h[j] = h[j], h[i] }
func (h *eventHeap) Push(x interface{}) { *h = append(*h, x.(*timer)) }
func (h *eventHeap) Pop() interface{} {
	old := *h
	n := len(old)
	t := old[n-1]
	*h = old[:n-1]
	return t
}

// message 是一份在途的状态推送；payload 在发送时深拷贝。
type message struct {
	from, to      string
	sendSeq       int
	sendAt, delay int
	payload       *crdt.ORSet
}

type inflightKey struct{ from, to string }

type node struct {
	state   *crdt.ORSet
	counter uint64
}

// Run 执行场景并返回确定性结果：同一场景同一 seed 的输出逐字节一致。
func Run(sc *Scenario) *Result {
	rng := rngFor(sc.Seed)

	nodes := make(map[string]*node, len(sc.Replicas))
	for _, r := range sc.Replicas {
		nodes[r] = &node{state: crdt.New()}
	}

	h := &eventHeap{}
	heap.Init(h)
	seq := 0

	// 本地事件按 (at, 声明顺序) 入堆；同 tick 的本地动作先后由场景声明顺序决定。
	scheduled := make([]EventSpec, len(sc.Events))
	copy(scheduled, sc.Events)
	sort.SliceStable(scheduled, func(i, j int) bool { return scheduled[i].At < scheduled[j].At })
	for _, e := range scheduled {
		seq++
		t := &timer{at: e.At, seq: seq, kind: "local:" + e.Kind, replica: e.Replica, value: e.Value}
		if e.Kind == "sync" {
			t.value = e.To
		}
		heap.Push(h, t)
	}

	var trace []TraceEntry
	stats := Stats{}
	sendSeq := 0
	inflight := make(map[inflightKey][]int)
	lastDeliveredSeq := make(map[inflightKey]int)

	// send 处理一条逻辑消息的丢包/重复/延迟/乱序判定并入队。
	send := func(fromName, toName string, at int) {
		from := nodes[fromName]
		sendSeq++
		sSeq := sendSeq
		payload := from.state.Clone()

		te := TraceEntry{At: at, Kind: "send", From: fromName, To: toName, SendSeq: sSeq}
		stats.MessagesSent++

		if rng.Float64() < sc.Network.DropProb {
			stats.MessagesDropped++
			te.Dropped = true
			trace = append(trace, te)
			return
		}
		duplicated := rng.Float64() < sc.Network.DuplicateProb
		if duplicated {
			stats.DuplicatesMade++
			te.Duplicated = true
		}
		forceReorder := rng.Float64() < sc.Network.ReorderProb
		if forceReorder {
			te.Reordered = true
		}
		trace = append(trace, te)

		key := inflightKey{fromName, toName}
		scheduleCopy := func(force bool) {
			lo, hi := sc.Network.MinDelay, sc.Network.MaxDelay
			delay := lo
			if hi > lo {
				delay = lo + rng.IntN(hi-lo+1)
			}
			arrival := at + delay
			forced := false
			// 强制乱序：让本消息早于该方向所有在途消息到达（“后发先至”）。
			if force && len(inflight[key]) > 0 {
				minArr := inflight[key][0]
				if arrival >= minArr {
					newArr := minArr - 1
					if newArr >= at { // 不引入负延迟
						arrival = newArr
						forced = true
					}
				}
			}
			if forced {
				stats.ForcedReorders++
			}
			inflight[key] = append(inflight[key], arrival)
			sort.Ints(inflight[key])
			seq++
			heap.Push(h, &timer{
				at: arrival, seq: seq, kind: "deliver",
				msg: &message{
					from: fromName, to: toName, sendSeq: sSeq,
					sendAt: at, delay: arrival - at, payload: payload,
				},
			})
		}
		scheduleCopy(forceReorder)
		if duplicated {
			// 重复副本独立抽取延迟，自身也可能自然造成乱序；不再递归判定丢包/重复。
			scheduleCopy(false)
		}
	}

	finalTick := 0
	for h.Len() > 0 {
		t := heap.Pop(h).(*timer)
		if t.at > finalTick {
			finalTick = t.at
		}
		switch t.kind {
		case "local:add":
			n := nodes[t.replica]
			n.counter++
			tag := crdt.UniqueTag{Origin: t.replica, Counter: n.counter}
			_ = n.state.AddWithTag(t.value, tag)
			stats.LocalAdds++
			trace = append(trace, TraceEntry{
				At: t.at, Kind: "add", Replica: t.replica, Value: t.value,
				Tag: tag.String(), Values: n.state.Values(),
			})
		case "local:remove":
			n := nodes[t.replica]
			removed, _ := n.state.RemoveObserved(t.value)
			stats.LocalRemoves++
			rs := make([]string, 0, len(removed))
			for _, x := range removed {
				rs = append(rs, x.String())
			}
			sort.Strings(rs)
			trace = append(trace, TraceEntry{
				At: t.at, Kind: "remove", Replica: t.replica, Value: t.value,
				Removed: rs, Values: n.state.Values(),
			})
		case "local:sync":
			send(t.replica, t.value, t.at)
		case "local:gossip":
			for _, other := range sc.Replicas {
				if other != t.replica {
					send(t.replica, other, t.at)
				}
			}
		case "deliver":
			m := t.msg
			key := inflightKey{m.from, m.to}
			arr := m.sendAt + m.delay
			if idx := sort.SearchInts(inflight[key], arr); idx < len(inflight[key]) && inflight[key][idx] == arr {
				inflight[key] = append(inflight[key][:idx], inflight[key][idx+1:]...)
			}
			n := nodes[m.to]
			before := n.state.Signature()
			n.state.Merge(m.payload)
			after := n.state.Signature()
			redundant := before == after
			reordered := m.sendSeq < lastDeliveredSeq[key] // 上一条已交付消息序号更大 => 后发先至
			if reordered {
				stats.Reorders++
			}
			lastDeliveredSeq[key] = m.sendSeq
			stats.MessagesDelivered++
			if redundant {
				stats.RedundantMerges++
			}
			trace = append(trace, TraceEntry{
				At: t.at, Kind: "deliver", From: m.from, To: m.to,
				SendSeq: m.sendSeq, Delay: m.delay, Reordered: reordered,
				Redundant: redundant, Values: n.state.Values(),
			})
		}
	}
	stats.FinalTick = finalTick

	res := &Result{
		Name: sc.Name, Seed: sc.Seed, Replicas: append([]string(nil), sc.Replicas...),
		FinalTick: finalTick, Stats: stats, Trace: trace,
		Nodes: make(map[string]NodeResult, len(sc.Replicas)),
	}

	stateSame, valuesSame := true, true
	var refState *crdt.ORSet
	var refValues []string
	for i, r := range sc.Replicas {
		st := nodes[r].state
		a, rr := st.Export()
		res.Nodes[r] = NodeResult{Values: st.Values(), A: a, R: rr}
		if i == 0 {
			refState = st
			refValues = st.Values()
		} else {
			if !st.Equal(refState) {
				stateSame = false
			}
			if !equalStrings(st.Values(), refValues) {
				valuesSame = false
			}
		}
	}
	res.Converged = stateSame
	res.ValuesConverged = valuesSame

	if len(sc.ExpectedValues) > 0 {
		res.ExpectedGiven = true
		res.ExpectedValues = append([]string(nil), sc.ExpectedValues...)
		res.MatchesExpected = stateSame && valuesSame && equalStrings(refValues, sc.ExpectedValues)
	}
	return res
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
