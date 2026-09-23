// Package sim 实现确定性的离散事件调度器（虚拟时钟，单位为 tick）。
//
// 所有事件（消息到达、定时器、故障注入）都进入按 (触发时刻, 入队序号)
// 排序的最小堆，因此：
//   - 同一输入 + 同一随机种子 => 逐事件完全可复现；
//   - 不依赖 wall clock、goroutine 时序或真实网络。
package sim

import (
	"container/heap"
	"math/rand"
)

// Event 是调度器内部的一个事件。
type Event struct {
	tick   int64
	seq    int64
	owner  string // 事件归属节点（崩溃时按 owner 撤销其全部待触发事件）
	sender string // 仅消息事件：发送方（发送方崩溃时撤销其在途消息）；其余为空
	kind   string // "timer" | "message" | "crash"，仅用于追踪输出
	fn     func()
}

// Scheduler 是虚拟时钟与事件堆。
type Scheduler struct {
	now    int64
	seq    int64
	events eventHeap
	rng    *rand.Rand
}

// New 创建调度器，seed 决定网络故障等所有随机行为。
func New(seed int64) *Scheduler {
	return &Scheduler{rng: rand.New(rand.NewSource(seed))}
}

// Now 返回当前虚拟时刻。
func (s *Scheduler) Now() int64 { return s.now }

// Rng 返回内部确定性随机源（网络层使用，业务代码不应自行随机）。
func (s *Scheduler) Rng() *rand.Rand { return s.rng }

// Schedule 在 now+delay 触发 fn。owner 为归属节点 ID，kind 仅用于追踪。
func (s *Scheduler) Schedule(owner, kind string, delay int64, fn func()) {
	s.ScheduleFrom(owner, "", kind, delay, fn)
}

// ScheduleFrom 调度一个带发送方标记的事件（消息到达事件使用）。
// owner=接收方（其崩溃则事件失效），sender=发送方（其崩溃则在途消息消失）。
func (s *Scheduler) ScheduleFrom(owner, sender, kind string, delay int64, fn func()) {
	if delay < 0 {
		delay = 0
	}
	s.seq++
	heap.Push(&s.events, &Event{
		tick:   s.now + delay,
		seq:    s.seq,
		owner:  owner,
		sender: sender,
		kind:   kind,
		fn:     fn,
	})
}

// CancelOwner 撤销归属 owner 的全部待触发事件（节点崩溃时调用：
// 该节点的所有定时器与发往它的在途消息一并丢失）。
// 返回撤销数量。
func (s *Scheduler) CancelOwner(owner string) int {
	kept := s.events[:0]
	n := 0
	for _, e := range s.events {
		if e.owner == owner {
			n++
			continue
		}
		kept = append(kept, e)
	}
	s.events = kept
	heap.Init(&s.events)
	return n
}

// CancelNode 模拟节点彻底崩溃：撤销该节点拥有的事件，以及它发出但尚在途的消息。
// 返回 (撤销的拥有事件数, 撤销的在途消息数)。
func (s *Scheduler) CancelNode(id string) (owned, inFlight int) {
	kept := s.events[:0]
	for _, e := range s.events {
		switch {
		case e.owner == id:
			owned++
		case e.sender == id:
			inFlight++
		default:
			kept = append(kept, e)
		}
	}
	s.events = kept
	heap.Init(&s.events)
	return
}

// PendingOwner 返回某节点待触发事件数（报告/调试用）。
func (s *Scheduler) PendingOwner(owner string) int {
	n := 0
	for _, e := range s.events {
		if e.owner == owner {
			n++
		}
	}
	return n
}

// Run 推进虚拟时钟，依次触发事件，直到超过 maxTick 或事件耗尽。
// 每个事件执行前先把时钟跳到事件时刻。
func (s *Scheduler) Run(maxTick int64) {
	for len(s.events) > 0 {
		e := heap.Pop(&s.events).(*Event)
		if e.tick > maxTick {
			// 超出观察窗口：放回（无实际意义）并结束。
			heap.Push(&s.events, e)
			s.now = maxTick
			return
		}
		s.now = e.tick
		e.fn()
	}
	s.now = maxTick
}

// ---- container/heap 适配 ----

type eventHeap []*Event

func (h eventHeap) Len() int { return len(h) }

// 同一时刻按入队序号排序，保证确定性。
func (h eventHeap) Less(i, j int) bool {
	if h[i].tick != h[j].tick {
		return h[i].tick < h[j].tick
	}
	return h[i].seq < h[j].seq
}

func (h eventHeap) Swap(i, j int) { h[i], h[j] = h[j], h[i] }

func (h *eventHeap) Push(x any) { *h = append(*h, x.(*Event)) }

func (h *eventHeap) Pop() any {
	old := *h
	n := len(old)
	e := old[n-1]
	old[n-1] = nil
	*h = old[:n-1]
	return e
}
