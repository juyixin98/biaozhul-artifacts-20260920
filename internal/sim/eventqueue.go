package sim

import "container/heap"

// eventKind 区分事件队列中的内部事件。
type eventKind int

const (
	evExternal  eventKind = -1   // 脚本注入的外部事件
	evDeliver   eventKind = iota // 一条消息到达目标节点
	evElection                   // 某节点选举超时
	evHeartbeat                  // 某节点心跳周期
)

type event struct {
	time  int64
	seq   int64 // 同刻 FIFO 序号，保证确定性
	kind  eventKind
	node  int   // evElection / evHeartbeat 所属节点
	epoch int64 // 排程时节点的“在世纪元”，崩溃重启后旧事件自动失效
	nonce int64 // evElection 的选举排程代号
	msg   *delayedMsg
}

type delayedMsg struct {
	from, to int
	payload  interface{} // raft.Message
	dup      bool        // 本次投递是否为重复副本
}

type eventHeap struct {
	items []*event
}

func (h eventHeap) Len() int { return len(h.items) }

func (h eventHeap) Less(i, j int) bool {
	if h.items[i].time != h.items[j].time {
		return h.items[i].time < h.items[j].time
	}
	return h.items[i].seq < h.items[j].seq
}

func (h eventHeap) Swap(i, j int) { h.items[i], h.items[j] = h.items[j], h.items[i] }

func (h *eventHeap) Push(x any) { h.items = append(h.items, x.(*event)) }

func (h *eventHeap) Pop() any {
	old := h.items
	n := len(old)
	it := old[n-1]
	h.items = old[:n-1]
	return it
}

// eventQueue 包装最小堆并负责分配单调序号。
type eventQueue struct {
	h   *eventHeap
	seq int64
}

func newEventQueue() *eventQueue {
	h := &eventHeap{}
	heap.Init(h)
	return &eventQueue{h: h}
}

func (q *eventQueue) push(e *event) {
	q.seq++
	e.seq = q.seq
	heap.Push(q.h, e)
}

func (q *eventQueue) pop() *event {
	if q.h.Len() == 0 {
		return nil
	}
	return heap.Pop(q.h).(*event)
}
