package executor

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"

	"worksteal/internal/clock"
	"worksteal/internal/event"
)

// Direct 是一个“提交即内联执行”的执行器，与 *Executor 实现同一组核心操作，
// 用于验证调度库对执行器实现可替换（测试中也可作为完全确定的执行环境）。
//
// 它不做工作窃取：Submit 在调用者 goroutine 上递归执行整棵任务树。
// Spawn 的子任务压入当前执行栈的本地队列，Wait 直接帮助执行直到目标完成。
// 因为所有任务都在调用者栈上运行，它天然是“单线程模式”的另一种极端形态。
type Direct struct {
	name string
	bus  *event.Bus
	clk  clock.Clock
	root context.Context
	stop context.CancelCauseFunc

	mu      sync.Mutex
	reg     map[string]*Task
	stopped bool

	wg sync.WaitGroup

	submitted atomic.Int64
	started   atomic.Int64
	completed atomic.Int64
	failed    atomic.Int64
	canceled  atomic.Int64
	spawned   atomic.Int64
}

// NewDirect 创建内联执行器。
func NewDirect(name string, clk clock.Clock, bus *event.Bus) *Direct {
	if name == "" {
		name = randomID("direct_", 4)
	}
	if clk == nil {
		clk = clock.NewReal()
	}
	if bus == nil {
		bus = event.NewBus(name)
	}
	root, cancel := context.WithCancelCause(context.Background())
	d := &Direct{name: name, bus: bus, clk: clk, root: root, stop: cancel, reg: map[string]*Task{}}
	d.bus.Emit(d.clk.Now(), event.KindExecutorStarted, "", "", -1, nil,
		map[string]any{"workers": 0, "deque": "inline"})
	return d
}

// Name 返回执行器名。
func (d *Direct) Name() string { return d.name }

// Bus 返回事件总线。
func (d *Direct) Bus() *event.Bus { return d.bus }

// Workers 返回 0（无独立 worker）。
func (d *Direct) Workers() int { return 0 }

// Submit 在当前 goroutine 上同步执行任务（及其派生树）。
func (d *Direct) Submit(fn Func, opts ...Option) (*Task, error) {
	d.mu.Lock()
	if d.stopped {
		d.mu.Unlock()
		return nil, ErrExecutorStopped
	}
	t, err := newTask(d.root, fn, nil, opts...)
	if err != nil {
		d.mu.Unlock()
		return nil, err
	}
	if _, dup := d.reg[t.id]; dup {
		d.mu.Unlock()
		return nil, errors.New("duplicate task id: " + t.id)
	}
	d.reg[t.id] = t
	d.wg.Add(1)
	d.mu.Unlock()
	d.submitted.Add(1)
	d.bus.Emit(d.clk.Now(), event.KindTaskSubmitted, t.id, "", -1, nil,
		map[string]any{"name": t.name})
	d.runInline(t, nil)
	return t, nil
}

// localStack 是 Direct 模式下当前执行路径的 LIFO 待办队列。
type localStack struct{ items []*Task }

func (s *localStack) push(t *Task) { s.items = append(s.items, t) }
func (s *localStack) pop() (*Task, bool) {
	n := len(s.items)
	if n == 0 {
		return nil, false
	}
	t := s.items[n-1]
	s.items[n-1] = nil
	s.items = s.items[:n-1]
	return t, true
}

func (d *Direct) runInline(t *Task, stack *localStack) {
	now := d.clk.Now()
	if !t.markRunning(now, 0, d.bus) {
		return
	}
	d.started.Add(1)
	if stack == nil {
		stack = &localStack{}
	}
	err := t.fn(t.ctx, &directContext{d: d, cur: t, stack: stack})
	// 若任务体 Wait 后仍有已派生未处理的兄弟任务（例如派生后未等待就返回，
	// 这在 Direct 语义下不允许丢任务），在返回前全部内联执行。
	for {
		child, ok := stack.pop()
		if !ok {
			break
		}
		d.runInline(child, stack)
	}
	d.finishInline(t, err)
}

func (d *Direct) finishInline(t *Task, err error) {
	now := d.clk.Now()
	cause := context.Cause(t.ctx)
	st := StatusCompleted
	switch {
	case cause != nil:
		st = StatusCanceled
		if err == nil {
			err = cause
		}
	case err != nil:
		st = StatusFailed
	}
	if t.terminalize(now, st, err, 0, true, d.bus) {
		switch st {
		case StatusCompleted:
			d.completed.Add(1)
		case StatusFailed:
			d.failed.Add(1)
		case StatusCanceled:
			d.canceled.Add(1)
		}
	}
	d.mu.Lock()
	delete(d.reg, t.id)
	d.mu.Unlock()
	d.wg.Done()
}

// Cancel 取消任务（排队中立即取消；运行中取消 ctx）。
func (d *Direct) Cancel(id string, reason error) bool {
	d.mu.Lock()
	t := d.reg[id]
	d.mu.Unlock()
	if t == nil || t.Status().Terminal() {
		return false
	}
	if reason == nil {
		reason = context.Canceled
	}
	if t.Status() == StatusQueued {
		if t.status.CompareAndSwap(int32(StatusQueued), int32(StatusCanceled)) {
			t.cancel(reason)
			t.finalizeQueuedCanceled(d.clk.Now(), reason, d.bus)
			d.canceled.Add(1)
			d.mu.Lock()
			delete(d.reg, t.id)
			d.mu.Unlock()
			d.wg.Done()
			return true
		}
		return false
	}
	t.cancel(reason)
	return true
}

// Wait 等待所有已提交任务终结。
func (d *Direct) Wait(ctx context.Context) error {
	done := make(chan struct{})
	go func() { d.wg.Wait(); close(done) }()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Shutdown 等待任务完成后关闭。
func (d *Direct) Shutdown(ctx context.Context) error {
	d.begin(false)
	if err := d.Wait(ctx); err != nil {
		return err
	}
	d.bus.Emit(d.clk.Now(), event.KindExecutorStopped, "", "", -1, nil,
		map[string]any{"graceful": true})
	return nil
}

// ShutdownNow 立即取消并关闭。
func (d *Direct) ShutdownNow() []Snapshot {
	d.begin(true)
	d.mu.Lock()
	pending := make([]Snapshot, 0, len(d.reg))
	for _, t := range d.reg {
		t.cancel(ErrExecutorStopped)
		pending = append(pending, t.Snapshot())
	}
	d.mu.Unlock()
	d.bus.Emit(d.clk.Now(), event.KindExecutorStopped, "", "", -1, nil,
		map[string]any{"graceful": false})
	return pending
}

func (d *Direct) begin(immediate bool) {
	d.mu.Lock()
	if !d.stopped {
		d.stopped = true
		if immediate {
			d.stop(ErrExecutorStopped)
		}
	}
	d.mu.Unlock()
	d.bus.Emit(d.clk.Now(), event.KindExecutorStopping, "", "", -1, nil,
		map[string]any{"immediate": immediate})
}

// Stats 返回计数快照（Direct 没有队列，Runnable 恒 0）。
func (d *Direct) Stats() Stats {
	d.mu.Lock()
	n := len(d.reg)
	d.mu.Unlock()
	return Stats{
		Submitted: d.submitted.Load(),
		Started:   d.started.Load(),
		Completed: d.completed.Load(),
		Failed:    d.failed.Load(),
		Canceled:  d.canceled.Load(),
		Spawned:   d.spawned.Load(),
		InFlight:  int64(n),
	}
}

// Snapshots 返回未终结任务快照。
func (d *Direct) Snapshots() []Snapshot {
	d.mu.Lock()
	out := make([]Snapshot, 0, len(d.reg))
	for _, t := range d.reg {
		out = append(out, t.Snapshot())
	}
	d.mu.Unlock()
	return out
}

// directContext 是 Direct 执行器的任务 Context：Spawn 压栈，Wait 排空栈
// 直到目标终结——与工作窃取版相同的 help-the-child 思想。
type directContext struct {
	d     *Direct
	cur   *Task // 正在执行的任务，子任务 ctx 从它派生
	stack *localStack
}

func (c *directContext) TaskID() string { return c.cur.id }

func (c *directContext) Spawn(fn Func, opts ...Option) (*Task, error) {
	c.d.mu.Lock()
	if c.d.stopped {
		c.d.mu.Unlock()
		return nil, ErrExecutorStopped
	}
	t, err := newTask(c.cur.ctx, fn, c.cur, opts...)
	if err != nil {
		c.d.mu.Unlock()
		return nil, err
	}
	if _, dup := c.d.reg[t.id]; dup {
		c.d.mu.Unlock()
		return nil, errors.New("duplicate task id: " + t.id)
	}
	c.d.reg[t.id] = t
	c.d.wg.Add(1)
	c.d.mu.Unlock()
	c.cur.addChild(t)
	c.d.spawned.Add(1)
	c.stack.push(t)
	c.d.bus.Emit(c.d.clk.Now(), event.KindTaskSpawned, t.id, c.cur.id, 0, nil,
		map[string]any{"name": t.name})
	return t, nil
}

func (c *directContext) Wait(target *Task) error {
	for {
		// 以 done 通道关闭判定终结，不能用 Err()!=nil（成功完成时 Err 为 nil）。
		select {
		case <-target.done:
			return target.Err()
		default:
		}
		if child, ok := c.stack.pop(); ok {
			c.d.runInline(child, c.stack)
			continue
		}
		// 目标不在本栈（例如等待外部提交的任务）：让出执行权。
		if c.d.isStopped() {
			return ErrExecutorStopped
		}
		<-target.done
		return target.Err()
	}
}

func (d *Direct) isStopped() bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.stopped
}
