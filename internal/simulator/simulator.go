package simulator

import (
	"container/heap"
	"fmt"
	"math/rand"
	"sort"
)

// ---- 内部消息与信道 ----

type msgKind int

const (
	mApp    msgKind = iota // 应用消息（转账）
	mMarker                // 快照标记
)

type message struct {
	kind   msgKind
	amount int64  // 应用消息金额
	snapID string // 标记所属快照
	id     int64  // 全局唯一消息 id
	dup    bool   // 是否为重复副本（best_effort 信道产生）
}

// channel 是一条有向链路 from -> to 的运行时状态。
type channel struct {
	from       string
	to         string
	policy     string
	base       int
	jitter     int
	lossPct    float64
	dupPct     float64
	reorder    bool
	sent       int
	delivered  int
	lost       int
	duplicated int
	reordered  int
}

// ---- 节点本地的每快照状态（Chandy-Lamport 本地侧记录）----

// chanSnapRec：某快照中，本节点一条入信道（来自 from）的记录。
type chanSnapRec struct {
	from    string
	closed  bool
	amounts []int64
}

// localSnap：节点本地关于某个快照的全部记录。
type localSnap struct {
	id       string
	recorded bool                    // 是否已记录本地状态
	balance  int64                   // 记录状态时刻的余额
	inChans  map[string]*chanSnapRec // 入信道 from -> 记录
}

type node struct {
	name    string
	balance int64
	snaps   map[string]*localSnap // 进行中的快照 id -> 本地记录
}

// ---- 事件堆 ----

type evKind int

const (
	evScript evKind = iota
	evDeliver
)

type simEvent struct {
	tick int
	seq  int // 同 tick 内的全局先后
	kind evKind
	ch   *channel
	msg  *message
	evt  *Event // 脚本事件
}

type evHeap []*simEvent

func (h evHeap) Len() int { return len(h) }
func (h evHeap) Less(i, j int) bool {
	if h[i].tick != h[j].tick {
		return h[i].tick < h[j].tick
	}
	return h[i].seq < h[j].seq
}
func (h evHeap) Swap(i, j int) { h[i], h[j] = h[j], h[i] }
func (h *evHeap) Push(x any)   { *h = append(*h, x.(*simEvent)) }
func (h *evHeap) Pop() any {
	old := *h
	n := len(old)
	x := old[n-1]
	*h = old[:n-1]
	return x
}

// ---- 模拟器 ----

type Simulator struct {
	seed       int64
	rng        *rand.Rand
	nodes      map[string]*node
	nodeOrder  []string
	channels   map[string]map[string]*channel // from -> to -> ch
	stats      map[*channel]*ChannelStat
	seq        int
	msgID      int64
	pq         evHeap
	log        []LogEntry
	tickLimit  int
	snapOrder  []string          // 快照发起顺序（完成后移除）
	initiators map[string]string // 快照 id -> 发起节点
	results    []SnapshotResult
}

// Execute 校验请求并执行一次确定性模拟。
func Execute(req *Request) (*Response, error) {
	if err := validate(req); err != nil {
		return nil, err
	}

	s := &Simulator{
		seed:       req.Seed,
		rng:        rand.New(rand.NewSource(req.Seed)),
		nodes:      map[string]*node{},
		channels:   map[string]map[string]*channel{},
		stats:      map[*channel]*ChannelStat{},
		initiators: map[string]string{},
	}

	for _, n := range req.Nodes {
		s.nodes[n.Name] = &node{name: n.Name, balance: n.Balance, snaps: map[string]*localSnap{}}
		s.nodeOrder = append(s.nodeOrder, n.Name)
	}
	for _, l := range req.Links {
		base := l.Base
		if base == 0 {
			base = 1
		}
		policy := l.Policy
		if policy == "" {
			policy = PolicyFIFO
		}
		ch := &channel{
			from: l.From, to: l.To, policy: policy, base: base, jitter: l.Jitter,
			lossPct: l.LossPct, dupPct: l.DupPct, reorder: l.Reorder,
		}
		if s.channels[l.From] == nil {
			s.channels[l.From] = map[string]*channel{}
		}
		s.channels[l.From][l.To] = ch
		s.stats[ch] = &ChannelStat{From: l.From, To: l.To, Policy: policy}
	}

	// 展开脚本事件（含周期重复）；同 tick 内按脚本出现顺序入堆
	maxTick := -1
	seq := 0
	pushScript := func(e Event) {
		heap.Push(&s.pq, &simEvent{kind: evScript, tick: e.Tick, seq: seq, evt: &e})
		seq++
		if e.Tick > maxTick {
			maxTick = e.Tick
		}
	}
	for _, e := range req.Events {
		if e.RepeatTo > e.Tick {
			step := e.RepeatEvery
			if step < 1 {
				step = 1
			}
			for t := e.Tick; t <= e.RepeatTo; t += step {
				ee := e
				ee.Tick = t
				ee.RepeatTo, ee.RepeatEvery = 0, 0
				pushScript(ee)
			}
		} else {
			pushScript(e)
		}
	}
	heap.Init(&s.pq)

	s.tickLimit = req.TickLimit
	if s.tickLimit == 0 {
		s.tickLimit = maxTick + 100
	}

	lastTick := s.run()
	return s.buildResponse(lastTick), nil
}

func (s *Simulator) nextSeq() int {
	v := s.seq
	s.seq++
	return v
}

func (s *Simulator) nextMsgID() int64 {
	s.msgID++
	return s.msgID
}

func (s *Simulator) run() int {
	lastTick := 0
	for s.pq.Len() > 0 {
		e := heap.Pop(&s.pq).(*simEvent)
		if e.tick >= s.tickLimit {
			heap.Push(&s.pq, e)
			break
		}
		lastTick = e.tick
		switch e.kind {
		case evScript:
			s.handleScript(e.tick, e.evt)
		case evDeliver:
			s.deliver(e.tick, e.ch, e.msg)
		}
		s.checkCompletions()
	}
	// 时限内未完成（如 best_effort 丢标记）的快照也输出，complete=false
	s.collectIncomplete()
	return lastTick
}

func (s *Simulator) handleScript(tick int, e *Event) {
	switch e.Kind {
	case KindTransfer:
		s.sendTransfer(tick, e.From, e.To, e.Amount)
	case KindMarker:
		s.initSnapshot(tick, e.From, e.SnapshotID)
	}
}

// sendTransfer：发送方扣款（余额不足拒绝，保证总量守恒），再把应用消息投入信道。
func (s *Simulator) sendTransfer(tick int, from, to string, amount int64) {
	n := s.nodes[from]
	if n.balance < amount {
		s.log = append(s.log, LogEntry{
			Tick: tick, Kind: "send", From: from, To: to, Amount: amount,
			Detail: "rejected: insufficient funds",
		})
		return
	}
	n.balance -= amount
	s.log = append(s.log, LogEntry{Tick: tick, Kind: "send", From: from, To: to, Amount: amount})
	s.emit(tick, s.channels[from][to], &message{kind: mApp, amount: amount, id: s.nextMsgID()})
}

// initSnapshot：n 记录本地状态并沿所有出信道发送标记。
// 多个节点可用同一 snapshot_id 发起（幂等：本机已有记录则忽略）。
func (s *Simulator) initSnapshot(tick int, nname, id string) {
	s.registerSnap(id, nname)
	n := s.nodes[nname]
	if _, ok := n.snaps[id]; ok {
		return
	}
	s.recordLocal(tick, nname, id)

	for _, to := range sortedChanKeys(s.channels[nname]) {
		ch := s.channels[nname][to]
		s.log = append(s.log, LogEntry{Tick: tick, Kind: "marker", From: nname, To: to, SnapID: id})
		s.emit(tick, ch, &message{kind: mMarker, snapID: id, id: s.nextMsgID()})
	}
}

func (s *Simulator) registerSnap(id, initiator string) {
	for _, x := range s.snapOrder {
		if x == id {
			return
		}
	}
	s.snapOrder = append(s.snapOrder, id)
	s.initiators[id] = initiator
}

// recordLocal：节点 nname 记录快照 id 的本地状态，为所有入信道建立记录。
func (s *Simulator) recordLocal(tick int, nname, id string) {
	n := s.nodes[nname]
	ls := &localSnap{
		id: id, recorded: true, balance: n.balance,
		inChans: map[string]*chanSnapRec{},
	}
	for from := range s.channels {
		if _, ok := s.channels[from][nname]; ok {
			ls.inChans[from] = &chanSnapRec{from: from}
		}
	}
	n.snaps[id] = ls
	s.log = append(s.log, LogEntry{
		Tick: tick, Kind: "snapshot", From: nname, SnapID: id,
		Detail: fmt.Sprintf("recorded state balance=%d", n.balance),
	})
}

// emit：把消息投入信道，按信道策略安排投递事件。
func (s *Simulator) emit(now int, ch *channel, msg *message) {
	st := s.stats[ch]
	st.Sent++
	if ch.policy == PolicyBestEffort {
		if pctHit(s.rng, ch.lossPct) {
			st.Lost++
			return
		}
		s.schedule(ch, msg, s.randomArrival(now, ch))
		if pctHit(s.rng, ch.dupPct) {
			st.Duplicated++
			dup := *msg
			dup.dup = true
			st.Sent++
			if pctHit(s.rng, ch.lossPct) {
				st.Lost++
			} else {
				s.schedule(ch, &dup, s.randomArrival(now, ch))
			}
		}
		return
	}

	// 可靠 FIFO：所有消息按 emit 顺序以固定延迟投递；
	// 事件堆同 tick 再按全局 seq 排序，因此顺序与发送顺序严格一致。
	s.schedule(ch, msg, now+ch.base)
}

func (s *Simulator) randomArrival(now int, ch *channel) int {
	delay := ch.base
	if ch.jitter > 0 {
		delay += s.rng.Intn(ch.jitter + 1)
	}
	arrival := now + delay
	// 30% 概率提前一个时间单位，制造对同信道在途消息的乱序
	if ch.reorder && arrival > now+1 && s.rng.Intn(10) < 3 {
		arrival--
		s.stats[ch].Reordered++
	}
	return arrival
}

func (s *Simulator) schedule(ch *channel, msg *message, arrival int) {
	heap.Push(&s.pq, &simEvent{
		kind: evDeliver, tick: arrival, seq: s.nextSeq(), ch: ch, msg: msg,
	})
}

func (s *Simulator) deliver(tick int, ch *channel, msg *message) {
	s.stats[ch].Delivered++
	switch msg.kind {
	case mMarker:
		s.onMarker(tick, ch, msg.snapID)
	case mApp:
		s.onAppMessage(tick, ch, msg.amount)
	}
}

func (s *Simulator) onAppMessage(tick int, ch *channel, amount int64) {
	to := s.nodes[ch.to]
	// 本节点已记录状态、且该入信道尚未收到本快照标记：消息计入在途消息。
	// 多个交叠快照各自独立记账，互不串台。
	for id, ls := range to.snaps {
		if !ls.recorded {
			continue
		}
		if cr := ls.inChans[ch.from]; cr != nil && !cr.closed {
			cr.amounts = append(cr.amounts, amount)
			s.log = append(s.log, LogEntry{
				Tick: tick, Kind: "deliver", From: ch.from, To: ch.to,
				Amount: amount, SnapID: id, Detail: "recorded in-flight",
			})
		}
	}
	to.balance += amount
	s.log = append(s.log, LogEntry{Tick: tick, Kind: "deliver", From: ch.from, To: ch.to, Amount: amount})
}

func (s *Simulator) onMarker(tick int, ch *channel, id string) {
	s.registerSnap(id, ch.to)
	n := s.nodes[ch.to]
	ls, exists := n.snaps[id]
	if !exists {
		// 首次收到该快照的标记：记录本地状态、沿出信道传播标记
		s.recordLocal(tick, ch.to, id)
		ls = n.snaps[id]
		for _, nxt := range sortedChanKeys(s.channels[ch.to]) {
			out := s.channels[ch.to][nxt]
			s.log = append(s.log, LogEntry{Tick: tick, Kind: "marker", From: ch.to, To: nxt, SnapID: id})
			s.emit(tick, out, &message{kind: mMarker, snapID: id, id: s.nextMsgID()})
		}
	}
	// 关闭该入信道：此后来自 ch.from 的消息不再计入本快照
	if cr := ls.inChans[ch.from]; cr != nil {
		cr.closed = true
	}
	s.log = append(s.log, LogEntry{
		Tick: tick, Kind: "marker", From: ch.from, To: ch.to, SnapID: id, Detail: "channel closed",
	})
}

// checkCompletions：所有节点都已记录状态、且所有入信道都已关闭时快照完成。
func (s *Simulator) checkCompletions() {
	completed := map[string]bool{}
	for _, id := range s.snapOrder {
		if s.isComplete(id) {
			// 必须先从节点本地记录构建结果，再清理本地状态
			s.results = append(s.results, s.buildResult(id, true, nil))
			completed[id] = true
		}
	}
	if len(completed) > 0 {
		for id := range completed {
			for _, n := range s.nodes {
				delete(n.snaps, id)
			}
		}
		kept := s.snapOrder[:0]
		for _, id := range s.snapOrder {
			if !completed[id] {
				kept = append(kept, id)
			}
		}
		s.snapOrder = kept
	}
}

func (s *Simulator) isComplete(id string) bool {
	for _, nname := range s.nodeOrder {
		ls := s.nodes[nname].snaps[id]
		if ls == nil {
			return false // 该节点尚未记录状态
		}
		for _, cr := range ls.inChans {
			if !cr.closed {
				return false
			}
		}
	}
	return true
}

func (s *Simulator) collectIncomplete() {
	var rest []SnapshotResult
	for _, id := range s.snapOrder {
		var missing []string
		for _, nname := range s.nodeOrder {
			ls := s.nodes[nname].snaps[id]
			if ls == nil {
				missing = append(missing, "state:"+nname)
				// 该节点未记录状态：所有入信道都未关闭
				for from := range s.channels {
					if _, ok := s.channels[from][nname]; ok {
						missing = append(missing, from+"->"+nname)
					}
				}
				continue
			}
			for _, cr := range ls.inChans {
				if !cr.closed {
					missing = append(missing, cr.from+"->"+nname)
				}
			}
		}
		rest = append(rest, s.buildResult(id, false, missing))
	}
	if len(rest) > 0 {
		s.results = append(s.results, rest...)
	}
	sort.SliceStable(s.results, func(i, j int) bool { return s.results[i].ID < s.results[j].ID })
}

// buildResult 汇总快照；missing 非 nil 时为不完整快照。
func (s *Simulator) buildResult(id string, complete bool, missing []string) SnapshotResult {
	r := SnapshotResult{
		ID: id, Complete: complete, States: map[string]int64{},
		InFlight: []ChannelSnap{}, Initiator: s.initiators[id], Missing: missing,
	}
	for _, nname := range s.nodeOrder {
		if ls := s.nodes[nname].snaps[id]; ls != nil {
			r.States[nname] = ls.balance
		}
	}
	for _, to := range s.nodeOrder {
		ls := s.nodes[to].snaps[id]
		if ls == nil {
			continue
		}
		for _, from := range s.nodeOrder {
			cr := ls.inChans[from]
			if cr == nil || len(cr.amounts) == 0 {
				continue
			}
			cs := ChannelSnap{From: from, To: to, Count: len(cr.amounts), Amounts: cr.amounts}
			for _, a := range cr.amounts {
				cs.Sum += a
			}
			r.InFlight = append(r.InFlight, cs)
		}
	}
	sort.Slice(r.InFlight, func(i, j int) bool {
		if r.InFlight[i].To != r.InFlight[j].To {
			return r.InFlight[i].To < r.InFlight[j].To
		}
		return r.InFlight[i].From < r.InFlight[j].From
	})
	if missing == nil {
		r.Missing = nil
	} else {
		sort.Strings(r.Missing)
	}
	for _, v := range r.States {
		r.StatesSum += v
	}
	for _, c := range r.InFlight {
		r.InFlightSum += c.Sum
	}
	r.Total = r.StatesSum + r.InFlightSum
	return r
}

func (s *Simulator) buildResponse(lastTick int) *Response {
	resp := &Response{
		Seed:          s.seed,
		LastTick:      lastTick,
		FinalBalances: map[string]int64{},
		Snapshots:     []SnapshotResult{},
		Log:           []LogEntry{},
	}
	for _, n := range s.nodeOrder {
		resp.FinalBalances[n] = s.nodes[n].balance
	}
	resp.Snapshots = s.results
	resp.Log = s.log
	for _, from := range s.nodeOrder {
		for _, to := range s.nodeOrder {
			if ch, ok := s.channels[from][to]; ok {
				resp.ChannelStats = append(resp.ChannelStats, *s.stats[ch])
			}
		}
	}
	return resp
}

// ---- 小工具 ----

func pctHit(rng *rand.Rand, pct float64) bool {
	return pct > 0 && rng.Float64()*100 < pct
}

func sortedChanKeys(m map[string]*channel) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
