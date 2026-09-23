package agingqueue

import (
	"context"
	"sort"
	"sync"
	"time"
)

// Timer 是 AfterFunc 返回的定时器句柄。
type Timer interface {
	// Stop 阻止定时器触发。返回 false 表示定时器已经触发或被停止过。
	Stop() bool
}

// Clock 是调度器依赖的全部时间操作。生产环境使用 RealClock，
// 测试使用可任意跃迁的 FakeClock。
type Clock interface {
	Now() time.Time
	// AfterFunc 在经过 d 后于独立调用栈执行 fn。
	//   - RealClock：由 time.AfterFunc 的后台 goroutine 执行；
	//   - FakeClock：在 Advance 跃迁越过到期点时"同步内联"执行
	//     （即在调用 Advance 的那个 goroutine 里、Advance 返回之前）。
	// 后者使基于假时钟的测试具备确定性：Advance 返回时，所有由该次
	// 时间推进触发的状态变更都已完成。
	AfterFunc(d time.Duration, fn func()) Timer
	// Sleep 等待 d，ctx 取消时提前返回 ctx.Err()。
	Sleep(ctx context.Context, d time.Duration) error
}

// RealClock 基于标准库 time 包实现。
type RealClock struct{}

func NewRealClock() *RealClock { return &RealClock{} }

func (RealClock) Now() time.Time { return time.Now() }

func (RealClock) AfterFunc(d time.Duration, fn func()) Timer {
	return time.AfterFunc(d, fn)
}

func (c RealClock) Sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return ctx.Err()
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

type fakeTimer struct {
	id      uint64
	at      time.Time
	fn      func()
	stopped bool
}

type fakeTimerHandle struct {
	clock *FakeClock
	id    uint64
}

func (h *fakeTimerHandle) Stop() bool {
	return h.clock.remove(h.id)
}

// FakeClock 是手动驱动的时钟：Now 只在 Advance 时变化，到期回调只在
// Advance（或注册一个已到期定时器）时于调用方 goroutine 内同步执行。
// 时间不允许回退。
type FakeClock struct {
	mu      sync.Mutex
	now     time.Time
	nextID  uint64
	pending []*fakeTimer // 未触发，按 at 升序
}

func NewFakeClock(start time.Time) *FakeClock {
	if start.IsZero() {
		start = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	}
	return &FakeClock{now: start}
}

func (f *FakeClock) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

// AfterFunc 注册定时器。已到期（d<=0 或 at<=now）的回调在解锁后于
// 当前 goroutine 同步执行（与 Advance 内联语义一致）。
func (f *FakeClock) AfterFunc(d time.Duration, fn func()) Timer {
	f.mu.Lock()
	f.nextID++
	t := &fakeTimer{id: f.nextID, at: f.now.Add(d), fn: fn}
	id := t.id
	dueNow := !t.at.After(f.now)
	if !dueNow {
		f.pending = append(f.pending, t)
		sort.Slice(f.pending, func(i, j int) bool {
			return f.pending[i].at.Before(f.pending[j].at)
		})
	}
	f.mu.Unlock()

	if dueNow {
		// 同步内联：锁外执行 fn（fn 可能回调 AfterFunc 重新取锁，安全）。
		f.mu.Lock()
		active := !t.stopped
		t.stopped = true
		f.mu.Unlock()
		if active {
			fn()
		}
	}
	return &fakeTimerHandle{clock: f, id: id}
}

// remove 撤销一个未触发的定时器；已触发返回 false。
func (f *FakeClock) remove(id uint64) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	for i, x := range f.pending {
		if x.id == id {
			f.pending = append(f.pending[:i], f.pending[i+1:]...)
			return true
		}
	}
	return false
}

func (f *FakeClock) Sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return ctx.Err()
	}
	done := make(chan struct{})
	t := f.AfterFunc(d, func() { close(done) })
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-done:
		return nil
	}
}

// Advance 把时钟向前推进 d，按到期顺序同步执行回调，并循环处理回调
// 期间新注册的到期定时器。
func (f *FakeClock) Advance(d time.Duration) {
	if d < 0 {
		panic("FakeClock: cannot advance backwards")
	}
	if d == 0 {
		return
	}
	f.mu.Lock()
	target := f.now.Add(d)
	f.now = target
	f.mu.Unlock()

	for {
		f.mu.Lock()
		var due []*fakeTimer
		for len(f.pending) > 0 && !f.pending[0].at.After(target) {
			due = append(due, f.pending[0])
			f.pending = f.pending[1:]
		}
		f.mu.Unlock()
		if len(due) == 0 {
			return
		}
		for _, t := range due {
			f.mu.Lock()
			active := !t.stopped
			t.stopped = true
			fn := t.fn
			f.mu.Unlock()
			if active {
				fn() // 同步内联执行（通常即调度器的 pump）
			}
		}
	}
}
