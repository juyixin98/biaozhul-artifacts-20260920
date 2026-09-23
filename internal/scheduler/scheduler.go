// Package scheduler 在执行器之上提供时间驱动的任务调度：一次性延迟任务
// 与固定间隔周期任务。时钟通过 clock.Clock 注入，测试中用 clock.Fake
// 确定性推进，无需真实睡眠。
package scheduler

import (
	"context"
	"errors"
	"strconv"
	"sync"
	"time"

	"worksteal/internal/clock"
	"worksteal/internal/event"
	"worksteal/internal/executor"
)

// Executor 是调度器依赖的执行器能力子集（*executor.Executor 天然满足）。
type Executor interface {
	Submit(fn executor.Func, opts ...executor.Option) (*executor.Task, error)
	Name() string
	Bus() *event.Bus
}

// Handle 是一个已排程任务的句柄。
type Handle struct {
	id       string
	name     string
	periodic bool
	cancel   func()
}

// ID 返回排程 ID。
func (h *Handle) ID() string { return h.id }

// Name 返回排程名称。
func (h *Handle) Name() string { return h.name }

// Periodic 报告是否为周期排程。
func (h *Handle) Periodic() bool { return h.periodic }

// Cancel 撤销排程：一次性任务尚未到期则不再提交；周期任务停止后续触发。
// 已经提交到执行器的任务不受影响（可用执行器 Cancel 取消）。
func (h *Handle) Cancel() { h.cancel() }

type entry struct {
	id     string
	name   string
	fn     executor.Func
	when   time.Time
	period time.Duration // 0 表示一次性
}

// Scheduler 持有待触发排程集合，到期时把任务提交给底层执行器。
//
// 单后台 goroutine 模型（loop）。每轮在锁内计算最早到期点并创建定时器，
// 随后释放锁，三路等待：定时器到期、排程变化（sync.Cond 桥接）、关闭。
// sync.Cond 保证“新增/取消排程”的唤醒不会因为早于等待而丢失；每轮新建
// 的桥接协程在被唤醒或关闭后立即退出，不产生协程泄漏。
type Scheduler struct {
	ex  Executor
	clk clock.Clock

	mu      sync.Mutex
	entries map[string]*entry
	nextID  int
	stopped bool
	cond    sync.Cond
	epoch   uint64 // 排程集合代次：变化时自增并广播

	stopCh chan struct{}
	doneCh chan struct{}
	ready  chan struct{}
}

// New 创建调度器并启动后台循环；返回时循环已完成首次等待装配。
func New(ex Executor, clk clock.Clock) *Scheduler {
	if clk == nil {
		clk = clock.NewReal()
	}
	s := &Scheduler{
		ex:      ex,
		clk:     clk,
		entries: make(map[string]*entry),
		stopCh:  make(chan struct{}),
		doneCh:  make(chan struct{}),
		ready:   make(chan struct{}),
	}
	s.cond.L = &s.mu
	go s.loop()
	<-s.ready
	return s
}

// After 安排 d 之后执行一次。d<0 按 0 处理（尽快执行）。
func (s *Scheduler) After(name string, d time.Duration, fn executor.Func) (*Handle, error) {
	if d < 0 {
		d = 0
	}
	return s.schedule(name, s.clk.Now().Add(d), 0, fn)
}

// At 在绝对时刻 t 执行一次。
func (s *Scheduler) At(name string, t time.Time, fn executor.Func) (*Handle, error) {
	return s.schedule(name, t, 0, fn)
}

// Every 每 d 触发一次的周期任务（首次触发在 d 之后）。
func (s *Scheduler) Every(name string, d time.Duration, fn executor.Func) (*Handle, error) {
	if d <= 0 {
		return nil, errors.New("period must be positive")
	}
	return s.schedule(name, s.clk.Now().Add(d), d, fn)
}

func (s *Scheduler) schedule(name string, when time.Time, period time.Duration, fn executor.Func) (*Handle, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.stopped {
		return nil, executor.ErrExecutorStopped
	}
	s.nextID++
	id := "sched-" + strconv.Itoa(s.nextID)
	e := &entry{id: id, name: name, fn: fn, when: when, period: period}
	h := &Handle{id: id, name: name, periodic: period > 0}
	h.cancel = func() {
		s.mu.Lock()
		delete(s.entries, id)
		s.epoch++
		s.mu.Unlock()
		s.cond.Broadcast()
	}
	s.entries[id] = e
	s.epoch++
	s.ex.Bus().Emit(s.clk.Now(), event.KindTaskScheduled, id, "", -1, nil,
		map[string]any{"schedule": name, "when": when, "period_ms": period.Milliseconds()})
	s.cond.Broadcast()
	return h, nil
}

// Kick 强制调度循环立即重新计算到期点并处理到期任务。
//
// 真实时钟下通常无需调用（排程增删会自动唤醒）；使用 clock.Fake 手工
// Advance 后应 Kick 一次：假时钟推进不产生真实时间流逝，循环靠 cond
// 唤醒才会重新挂定时器并发现已到期任务。
func (s *Scheduler) Kick() {
	s.mu.Lock()
	s.epoch++
	s.mu.Unlock()
	s.cond.Broadcast()
}

// changed 在持有锁的情况下创建一个“排程代次发生变化或关闭”的通道。
// 桥接协程短暂持有 cond 等待，唤醒后立即退出。
func (s *Scheduler) changed(seen uint64) <-chan struct{} {
	ch := make(chan struct{})
	go func() {
		s.mu.Lock()
		defer s.mu.Unlock()
		for !s.stopped && s.epoch == seen {
			s.cond.Wait()
		}
		close(ch)
	}()
	return ch
}

// loop 是唯一的常驻后台 goroutine。
func (s *Scheduler) loop() {
	defer close(s.doneCh)

	s.mu.Lock()
	close(s.ready)
	for {
		if s.stopped {
			s.mu.Unlock()
			return
		}
		// 唤醒后先排空所有到期任务（假时钟一次推进可能跨过多个周期点）。
		// 必须在锁内反复检查，直到最早到期点严格在未来，再挂定时器等待。
		for s.hasDueLocked() {
			s.mu.Unlock()
			s.fireDue()
			s.mu.Lock()
			if s.stopped {
				s.mu.Unlock()
				return
			}
		}
		seen := s.epoch
		timerC, cancelTimer := s.armTimerLocked()
		s.mu.Unlock()
		// 锁外创建 cond 桥接（它自行取锁等待），避免与本循环的锁嵌套。
		changed := s.changed(seen)

		select {
		case <-s.stopCh:
			cancelTimer()
			return
		case <-timerC:
			cancelTimer()
		case <-changed:
			// 排程变化或 Kick：取消定时器，回到循环顶部排空并重挂。
			cancelTimer()
		}
		s.mu.Lock()
	}
}

// hasDueLocked 报告当前时刻是否存在已到期排程。
func (s *Scheduler) hasDueLocked() bool {
	now := s.clk.Now()
	for _, e := range s.entries {
		if !e.when.After(now) {
			return true
		}
	}
	return false
}

// armTimerLocked 调用方持有 s.mu。返回一个只可能收到一次当前定时器
// 触发事件的通道，以及取消函数。关键作用：旧定时器通道里残留的触发值
// （fake 定时器容量为 1，Stop 不清空）不会泄露到下一轮——每轮的转发
// 协程和输出通道都是新建的，取消后协程退出即被丢弃。
func (s *Scheduler) armTimerLocked() (<-chan struct{}, func()) {
	best := s.earliestLocked()
	if best == nil {
		return nil, func() {}
	}
	d := best.when.Sub(s.clk.Now())
	if d < 0 {
		d = 0
	}
	timer := s.clk.NewTimer(d)
	out := make(chan struct{}, 1)
	stop := make(chan struct{})
	go func() {
		select {
		case <-timer.C():
			select {
			case out <- struct{}{}:
			default:
			}
		case <-stop:
		case <-s.stopCh:
		}
	}()
	cancel := func() {
		timer.Stop()
		select {
		case <-stop:
		default:
			close(stop)
		}
	}
	return out, cancel
}

func (s *Scheduler) earliestLocked() *entry {
	var best *entry
	for _, e := range s.entries {
		if best == nil || e.when.Before(best.when) {
			best = e
		}
	}
	return best
}

// takeDue 锁内弹出所有到期条目；周期任务重排到下一周期，一次性删除。
// 返回在锁外提交的条目，避免持锁调用执行器。
func (s *Scheduler) takeDue() []*entry {
	now := s.clk.Now()
	s.mu.Lock()
	var due []*entry
	for id, e := range s.entries {
		if !e.when.After(now) {
			due = append(due, e)
			if e.period > 0 {
				e.when = e.when.Add(e.period)
				if !e.when.After(now) {
					e.when = now.Add(e.period)
				}
			} else {
				delete(s.entries, id)
			}
		}
	}
	s.mu.Unlock()
	return due
}

func (s *Scheduler) fireDue() {
	for _, e := range s.takeDue() {
		if _, err := s.ex.Submit(e.fn, executor.WithName(e.name)); err != nil {
			// 执行器已关闭：删除可能仍在册的周期条目。
			s.mu.Lock()
			delete(s.entries, e.id)
			s.mu.Unlock()
		}
	}
}

// Pending 返回活跃排程数量（测试辅助）。
func (s *Scheduler) Pending() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.entries)
}

// Shutdown 停止调度循环：未到期任务不再提交；周期任务停止触发。
// 不影响已提交给执行器的任务。
func (s *Scheduler) Shutdown(_ context.Context) {
	s.mu.Lock()
	if s.stopped {
		s.mu.Unlock()
		<-s.doneCh
		return
	}
	s.stopped = true
	s.entries = make(map[string]*entry)
	s.mu.Unlock()
	s.cond.Broadcast()
	close(s.stopCh)
	<-s.doneCh
}
