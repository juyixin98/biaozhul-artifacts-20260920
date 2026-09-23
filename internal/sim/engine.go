// Package sim 提供确定性离散事件模拟器：全局事件堆 + 可播种随机数。
package sim

import (
	"container/heap"
	"math/rand"
)

// Action 是事件触发时执行的回调。
type Action func(now int64)

type item struct {
	seq    int64 // 同分（同刻、同优先级）时的插入序号，保证确定性
	when   int64
	pri    int
	action Action
}

type pq []*item

func (p pq) Len() int { return len(p) }
func (p pq) Less(i, j int) bool {
	if p[i].when != p[j].when {
		return p[i].when < p[j].when
	}
	if p[i].pri != p[j].pri {
		return p[i].pri < p[j].pri
	}
	return p[i].seq < p[j].seq
}
func (p pq) Swap(i, j int) { p[i], p[j] = p[j], p[i] }
func (p *pq) Push(x any)   { *p = append(*p, x.(*item)) }
func (p *pq) Pop() any {
	old := *p
	n := len(old)
	it := old[n-1]
	old[n-1] = nil
	*p = old[:n-1]
	return it
}

// Engine 是确定性的单进程离散事件引擎。
type Engine struct {
	now      int64
	seq      int64
	q        pq
	rng      *rand.Rand
	deadline int64 // 仿真截止时刻；<=0 表示无截止
	stopped  bool
}

// NewEngine 创建引擎；seed 决定所有随机行为。
func NewEngine(seed int64) *Engine {
	e := &Engine{rng: rand.New(rand.NewSource(seed))}
	heap.Init(&e.q)
	return e
}

// SetDeadline 设置仿真截止时刻（Run 在 now > deadline 时停止）。
func (e *Engine) SetDeadline(t int64) { e.deadline = t }

// Now 返回当前逻辑时钟。
func (e *Engine) Now() int64 { return e.now }

// RNG 返回引擎内部的随机源（不要在它之外另建随机源，否则会破坏确定性/可复现性）。
func (e *Engine) RNG() *rand.Rand { return e.rng }

// ScheduleAt 在绝对时刻 when 调度事件。同刻事件按 pri 升序执行，再按调度先后执行。
func (e *Engine) ScheduleAt(when int64, pri int, a Action) {
	if when < e.now {
		when = e.now
	}
	e.seq++
	heap.Push(&e.q, &item{seq: e.seq, when: when, pri: pri, action: a})
}

// Schedule 在 dt 个逻辑时间单位后调度事件。
func (e *Engine) Schedule(dt int64, pri int, a Action) {
	e.ScheduleAt(e.now+dt, pri, a)
}

// Stop 请求引擎在当前事件处理完毕后停止（不再派发后续事件）。
func (e *Engine) Stop() { e.stopped = true }

// Pending 报告尚未派发的事件数。
func (e *Engine) Pending() int { return e.q.Len() }

// Run 依次派发事件，直到队列空、被 Stop 或越过截止时刻。
func (e *Engine) Run() {
	for e.q.Len() > 0 && !e.stopped {
		it := heap.Pop(&e.q).(*item)
		if e.deadline > 0 && it.when > e.deadline {
			// 越过截止时刻的事件重新放回队列但停止运行；保持 now 为最后执行时刻。
			heap.Push(&e.q, it)
			return
		}
		e.now = it.when
		it.action(e.now)
	}
}
