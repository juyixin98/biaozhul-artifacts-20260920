package rt

import (
	"sort"
	"sync"
	"time"
)

// Clock 是可注入的时钟。RTO 等所有时间相关逻辑只通过它获取，
// 从而测试中可以用 FakeClock 确定性地推进时间，完全不 sleep。
type Clock interface {
	Now() time.Time
	// NewTimer 创建一个一次性定时器，行为与 time.Timer 一致：
	// C 在到期时收到一个时间值；Stop 尽力阻止触发，
	// 返回 false 表示定时器已经到期或被停止。
	NewTimer(d time.Duration) Timer
}

// Timer 是 time.Timer 的抽象。
type Timer interface {
	C() <-chan time.Time
	Stop() bool
	// Reset 使定时器重新计时 d。返回 false 表示定时器已到期/已停止，
	// true 表示被复用。语义与 time.Timer.Reset 相同（调用方需自行排空）。
	Reset(d time.Duration) bool
}

// RealClock 包装标准库时间。
type RealClock struct{}

func (RealClock) Now() time.Time { return time.Now() }

func (RealClock) NewTimer(d time.Duration) Timer { return &realTimer{t: time.NewTimer(d)} }

type realTimer struct {
	t *time.Timer
}

func (r *realTimer) C() <-chan time.Time { return r.t.C }
func (r *realTimer) Stop() bool          { return r.t.Stop() }
func (r *realTimer) Reset(d time.Duration) bool {
	return r.t.Reset(d)
}

// FakeClock 是手工推进的虚拟时钟。NewTimer 得到的定时器只在
// Advance / tickToNext 被调用时才可能触发，测试因此无需真实 sleep。
//
// 可以安全地与协议 goroutine 并发使用。
type FakeClock struct {
	mu      sync.Mutex
	now     time.Time
	pending []*fakeTimer
}

// NewFakeClock 创建虚拟时钟，起始时刻为 time.Unix(0, 0)。
func NewFakeClock() *FakeClock {
	return &FakeClock{now: time.Unix(0, 0)}
}

func (c *FakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *FakeClock) NewTimer(d time.Duration) Timer {
	c.mu.Lock()
	defer c.mu.Unlock()
	t := &fakeTimer{
		clock:    c,
		deadline: c.now.Add(d),
		ch:       make(chan time.Time, 1),
	}
	c.pending = append(c.pending, t)
	return t
}

// Advance 把虚拟时间向前推进 d，并按到期先后触发所有到期定时器。
// 推进期间新产生（Reset 到到期点）的定时器也会被触发。
func (c *FakeClock) Advance(d time.Duration) {
	end := c.Now().Add(d)
	for {
		c.mu.Lock()
		var due []*fakeTimer
		for _, t := range c.pending {
			t.mu.Lock()
			if !t.stopped && !t.timedOut && !t.deadline.After(end) {
				due = append(due, t)
			}
			t.mu.Unlock()
		}
		if len(due) == 0 {
			c.now = end
			c.mu.Unlock()
			return
		}
		sort.Slice(due, func(i, j int) bool { return due[i].deadline.Before(due[j].deadline) })
		next := due[0]
		c.now = next.deadline
		c.mu.Unlock()

		next.fire(c.now)
	}
}

// Step 把虚拟时钟推进一小步（若有更早到期的定时器则只走到那里），
// 并触发该时刻全部到期的定时器。返回 true 表示本次有定时器被触发。
//
// 与“直接跳到下一个定时器”相比，固定小步进配合测试循环里的调度
// 让出，能保证协议 goroutine 在两次虚拟到期之间真实运行，
// 不会因为虚拟时钟冲得太快而把可恢复的丢包误判为连续超时。
func (c *FakeClock) Step(maxStep time.Duration) bool {
	c.mu.Lock()
	var earliest time.Time
	found := false
	for _, t := range c.pending {
		t.mu.Lock()
		if !t.stopped && !t.timedOut {
			if !found || t.deadline.Before(earliest) {
				earliest, found = t.deadline, true
			}
		}
		t.mu.Unlock()
	}
	if !found {
		c.mu.Unlock()
		return false
	}
	target := c.now.Add(maxStep)
	if earliest.Before(target) {
		target = earliest
	}
	c.now = target
	var due []*fakeTimer
	for _, t := range c.pending {
		t.mu.Lock()
		if !t.stopped && !t.timedOut && !t.deadline.After(target) {
			due = append(due, t)
		}
		t.mu.Unlock()
	}
	c.mu.Unlock()
	for _, t := range due {
		t.fire(target)
	}
	return len(due) > 0
}

// HasPending 报告当前是否还有未触发的定时器。
func (c *FakeClock) HasPending() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, t := range c.pending {
		t.mu.Lock()
		if !t.stopped && !t.timedOut {
			t.mu.Unlock()
			return true
		}
		t.mu.Unlock()
	}
	return false
}

type fakeTimer struct {
	clock    *FakeClock
	mu       sync.Mutex
	deadline time.Time
	ch       chan time.Time
	stopped  bool
	timedOut bool // 已经触发过（一次性）
}

func (t *fakeTimer) C() <-chan time.Time { return t.ch }

func (t *fakeTimer) Stop() bool {
	t.mu.Lock()
	active := !t.stopped && !t.timedOut
	t.stopped = true
	t.mu.Unlock()
	return active
}

func (t *fakeTimer) Reset(d time.Duration) bool {
	t.mu.Lock()
	active := !t.stopped && !t.timedOut
	t.stopped = false
	t.timedOut = false
	c := t.clock
	t.mu.Unlock()

	c.mu.Lock()
	t.mu.Lock()
	t.deadline = c.now.Add(d)
	t.mu.Unlock()
	c.mu.Unlock()
	return active
}

// fire 标记到期并尝试投递。ch 容量为 1，旧值未被取走时本次触发丢弃，
// 这与 time.Timer “通道里最多一个值” 的行为一致。
func (t *fakeTimer) fire(now time.Time) {
	t.mu.Lock()
	if t.stopped || t.timedOut {
		t.mu.Unlock()
		return
	}
	t.timedOut = true
	t.stopped = true
	t.mu.Unlock()

	select {
	case t.ch <- now:
	default:
	}
}
