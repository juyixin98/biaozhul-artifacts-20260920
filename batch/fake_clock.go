package batch

import (
	"sync"
	"time"
)

// FakeClock 是确定性的虚拟时钟：时间只在调用 Advance 时向前走，
// 到时的定时器在 Advance 中同步触发（发送使用缓冲为 1 的通道，
// 因此即使此刻没有接收者也不会阻塞或丢失）。
type FakeClock struct {
	mu     sync.Mutex
	now    time.Time
	timers map[int]*fakeTimer
	nextID int
}

// NewFakeClock 从时间 t 开始构造虚拟时钟；传入零值则使用 time.Unix(0, 0)。
func NewFakeClock(t time.Time) *FakeClock {
	if t.IsZero() {
		t = time.Unix(0, 0)
	}
	return &FakeClock{now: t, timers: make(map[int]*fakeTimer)}
}

func (c *FakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

// NewTimer 创建一个 d 后到期的定时器。d <= 0 会立即到期。
func (c *FakeClock) NewTimer(d time.Duration) Timer {
	c.mu.Lock()
	id := c.nextID
	c.nextID++
	ft := &fakeTimer{
		clock: c,
		id:    id,
		ch:    make(chan time.Time, 1),
		next:  c.now.Add(d),
	}
	if d <= 0 {
		ft.fired = true
		ft.ch <- c.now
	}
	c.timers[id] = ft
	c.mu.Unlock()
	return ft
}

// Advance 将虚拟时间推进 d，并同步触发所有到期时间不超过新时间、
// 且尚未触发的定时器（按到期时间升序，同刻按创建顺序）。
func (c *FakeClock) Advance(d time.Duration) {
	if d <= 0 {
		return
	}
	c.mu.Lock()
	c.now = c.now.Add(d)
	now := c.now

	type pending struct {
		at time.Time
		ft *fakeTimer
	}
	var due []pending
	for _, ft := range c.timers {
		if !ft.fired && !ft.stopped && !ft.next.After(now) {
			due = append(due, pending{ft.next, ft})
		}
	}
	// 简单的稳定排序：数量通常很少（每个活跃合批键一个等待定时器）。
	for i := 1; i < len(due); i++ {
		for j := i; j > 0 && due[j-1].at.After(due[j].at); j-- {
			due[j-1], due[j] = due[j], due[j-1]
		}
	}
	for _, p := range due {
		ft := p.ft
		if ft.fired || ft.stopped {
			continue
		}
		ft.fired = true
		delete(c.timers, ft.id)
		// 缓冲为 1：定时器已触发的情况下 Reset 会先排空通道，
		// 此处发送不会阻塞。
		ft.ch <- now
	}
	c.mu.Unlock()
}

type fakeTimer struct {
	clock   *FakeClock
	id      int
	ch      chan time.Time
	next    time.Time
	fired   bool
	stopped bool
}

func (t *fakeTimer) C() <-chan time.Time { return t.ch }

// Stop 阻止定时器触发。返回值语义与 time.Timer.Stop 对齐：
// 定时器尚未触发且未被停止时返回 true。
func (t *fakeTimer) Stop() bool {
	c := t.clock
	c.mu.Lock()
	defer c.mu.Unlock()
	if t.fired || t.stopped {
		return false
	}
	t.stopped = true
	delete(c.timers, t.id)
	return true
}

// Reset 将到期时间改为从当前虚拟时间起的 d 之后。
// 语义与 time.Timer.Reset 对齐：复用的定时器必须已停止或已触发。
func (t *fakeTimer) Reset(d time.Duration) bool {
	c := t.clock
	c.mu.Lock()
	active := !t.fired && !t.stopped
	if t.fired {
		// 排空已发送但尚未被接收的到期信号。
		select {
		case <-t.ch:
		default:
		}
	}
	t.stopped = false
	t.fired = false
	t.next = c.now.Add(d)
	if d <= 0 {
		t.fired = true
		t.ch <- c.now
	} else {
		c.timers[t.id] = t
	}
	c.mu.Unlock()
	return active
}
