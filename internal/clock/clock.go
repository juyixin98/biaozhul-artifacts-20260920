// Package clock 定义可替换的调度时钟，使依赖时间的调度逻辑可在测试中确定性推进。
package clock

import (
	"sync"
	"time"
)

// Clock 抽象执行器与调度器所需的时间操作。生产环境使用 Real，测试使用 Fake。
type Clock interface {
	Now() time.Time
	// NewTimer 返回一个在相对时长 d 后触发的定时器。
	// d <= 0 时定时器必须立即（或尽快）触发。
	NewTimer(d time.Duration) Timer
}

// Timer 是 time.Timer 的可替换版本。
type Timer interface {
	// C 返回触发通道。每次 NewTimer 调用返回的定时器都有自己独立的通道。
	C() <-chan time.Time
	// Stop 阻止定时器触发。返回 false 表示定时器已触发或已被停止。
	Stop() bool
}

// Real 是基于系统墙钟的真实时钟。
type Real struct{}

// NewReal 返回真实时钟（无状态，返回零值即可）。
func NewReal() Real { return Real{} }

func (Real) Now() time.Time { return time.Now() }

func (Real) NewTimer(d time.Duration) Timer {
	t := time.NewTimer(d)
	return &realTimer{t: t}
}

type realTimer struct {
	t *time.Timer
}

func (r *realTimer) C() <-chan time.Time { return r.t.C }
func (r *realTimer) Stop() bool          { return r.t.Stop() }

// Fake 是手工推进的确定性时钟，供调度器测试使用。
//
// 未触发的定时器按触发时间保存在最小堆中；Advance 推进当前时刻并触发
// 所有到期定时器。触发时间相同的定时器按注册顺序触发。
type Fake struct {
	mu      sync.Mutex
	now     time.Time
	pending []*fakeTimer
	seq     int64
}

// NewFake 以给定起始时刻创建假时钟。
func NewFake(start time.Time) *Fake {
	return &Fake{now: start}
}

func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

// NewTimer 创建相对时长的定时器。d<0 与 d=0 等价：创建即到期。
func (f *Fake) NewTimer(d time.Duration) Timer {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.seq++
	if d < 0 {
		d = 0
	}
	t := &fakeTimer{
		c:     make(chan time.Time, 1),
		when:  f.now.Add(d),
		seq:   f.seq,
		clock: f,
	}
	f.pending = append(f.pending, t)
	f.up(len(f.pending) - 1)
	return t
}

// Advance 将时钟向前推进 d，并同步触发所有到期定时器（在持有锁的状态下
// 向容量为 1 的缓冲通道投递，不会阻塞），返回推进后的时刻。
func (f *Fake) Advance(d time.Duration) time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.now = f.now.Add(d)
	for len(f.pending) > 0 {
		t := f.pending[0]
		if t.when.After(f.now) {
			break
		}
		// 弹出堆顶（无论它是否已被 Stop：停止项不投递，但要移出堆，
		// 否则它会一直挡在堆顶让后面的好定时器永远无法触发）。
		last := len(f.pending) - 1
		f.pending[0] = f.pending[last]
		f.pending[last] = nil
		f.pending = f.pending[:last]
		if last > 0 {
			f.down(0)
		}
		if t.stopped {
			continue
		}
		t.fired = true
		select {
		case t.c <- t.when:
		default: // 通道已有待取的触发值，丢弃重复值。
		}
	}
	return f.now
}

// Pending 返回尚未触发（且未停止）的定时器数量，测试辅助方法。
func (f *Fake) Pending() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, t := range f.pending {
		if !t.stopped && !t.fired {
			n++
		}
	}
	return n
}

type fakeTimer struct {
	c       chan time.Time
	when    time.Time
	seq     int64
	stopped bool
	fired   bool
	clock   *Fake
}

func (t *fakeTimer) C() <-chan time.Time { return t.c }

func (t *fakeTimer) Stop() bool {
	f := t.clock
	f.mu.Lock()
	defer f.mu.Unlock()
	if t.stopped || t.fired {
		return false
	}
	t.stopped = true
	return true
}

// less: 触发时间升序，相同时间按注册顺序升序。
func (f *Fake) less(i, j int) bool {
	a, b := f.pending[i], f.pending[j]
	if a.when.Equal(b.when) {
		return a.seq < b.seq
	}
	return a.when.Before(b.when)
}

func (f *Fake) up(i int) {
	for i > 0 {
		p := (i - 1) / 2
		if !f.less(i, p) {
			return
		}
		f.pending[p], f.pending[i] = f.pending[i], f.pending[p]
		i = p
	}
}

func (f *Fake) down(i int) {
	n := len(f.pending)
	for {
		l := 2*i + 1
		if l >= n {
			return
		}
		small := l
		if r := l + 1; r < n && f.less(r, l) {
			small = r
		}
		if !f.less(small, i) {
			return
		}
		f.pending[small], f.pending[i] = f.pending[i], f.pending[small]
		i = small
	}
}
