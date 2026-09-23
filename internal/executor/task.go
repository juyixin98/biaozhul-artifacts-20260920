package executor

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"runtime/debug"
	"sync"
	"sync/atomic"
	"time"

	"worksteal/internal/event"
)

// Status 是任务的生命周期状态。
type Status int32

const (
	// StatusQueued：任务已提交，尚未被任何 worker 认领执行。
	StatusQueued Status = iota
	// StatusRunning：恰好有一个 worker 正在执行任务体。
	StatusRunning
	// StatusCompleted：任务体正常返回 nil。
	StatusCompleted
	// StatusFailed：任务体返回非 nil 错误或 panic（未被取消时）。
	StatusFailed
	// StatusCanceled：提交前被取消（从未执行），或执行中收到取消。
	StatusCanceled
)

func (s Status) String() string {
	switch s {
	case StatusQueued:
		return "queued"
	case StatusRunning:
		return "running"
	case StatusCompleted:
		return "completed"
	case StatusFailed:
		return "failed"
	case StatusCanceled:
		return "canceled"
	default:
		return "unknown"
	}
}

// Terminal 报告状态是否为终态。
func (s Status) Terminal() bool {
	return s == StatusCompleted || s == StatusFailed || s == StatusCanceled
}

// PanicError 包装任务体 panic 的值与堆栈。
type PanicError struct {
	Value any
	Stack []byte
}

func (e *PanicError) Error() string {
	return fmt.Sprintf("task panicked: %v", e.Value)
}

// ErrExecutorStopped 在执行器已停止后提交/等待任务时返回。
var ErrExecutorStopped = errors.New("executor stopped")

// Func 是任务体。ctx 携带取消信号；c 用于在任务体内派生子任务。
type Func func(ctx context.Context, c Context) error

// Option 配置单个任务。
type Option func(*Task)

// WithID 指定任务 ID（默认随机生成；重复 ID 在同一执行器内会被拒绝）。
func WithID(id string) Option {
	return func(t *Task) { t.id = id }
}

// WithName 给任务一个可读名称（出现在事件 payload 中）。
func WithName(name string) Option {
	return func(t *Task) { t.name = name }
}

// Task 是任务的句柄与状态载体。
//
// 任务指针同时被双端队列、注册表和调用方持有；并发安全由内部锁保证。
type Task struct {
	id   string
	name string
	fn   Func
	sub  time.Time // 提交时刻（事件时间戳由 bus 记录，这里用于快照）

	// 生命周期。
	status atomic.Int32
	// cancel 取消任务自身 ctx；canceledOnce 保证 cancel 只传播一次。
	ctx    context.Context
	cancel context.CancelCauseFunc
	done   chan struct{}

	// errMu 保护 err、result 及 finished 元数据。
	mu       sync.Mutex
	err      error
	started  time.Time
	finished time.Time
	worker   int
	// runs 实际进入任务体的次数；验收要求它恒为 0 或 1。
	runs int
	// parent 派生该任务的任务；外部提交为 nil。
	parent *Task
	// children 当前仍未终结的子任务集合，用于取消传播。
	children map[*Task]struct{}
}

func newTask(ctx context.Context, fn Func, parent *Task, opts ...Option) (*Task, error) {
	t := &Task{
		fn:       fn,
		parent:   parent,
		children: make(map[*Task]struct{}),
		done:     make(chan struct{}),
	}
	for _, o := range opts {
		o(t)
	}
	if t.id == "" {
		t.id = randomID("t_", 6)
	}
	if t.name == "" {
		t.name = t.id
	}
	t.ctx, t.cancel = context.WithCancelCause(ctx)
	t.status.Store(int32(StatusQueued))
	return t, nil
}

func randomID(prefix string, n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		// crypto/rand 失败极为罕见；退化为时间戳避免返回错误影响 API。
		return fmt.Sprintf("%s%d", prefix, time.Now().UnixNano())
	}
	return prefix + hex.EncodeToString(b)
}

// ID 返回任务 ID。
func (t *Task) ID() string { return t.id }

// Name 返回任务名。
func (t *Task) Name() string { return t.name }

// Status 返回当前状态的快照。
func (t *Task) Status() Status { return Status(t.status.Load()) }

// Err 返回任务的终结错误：取消为 context.Canceled（或带 cause），
// panic 包装为 *PanicError；成功或未终结时为 nil。
func (t *Task) Err() error {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.err
}

// Runs 返回任务体被进入的次数。正确性契约：恒为 0 或 1。
func (t *Task) Runs() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.runs
}

// Done 在任务进入终态时关闭。
func (t *Task) Done() <-chan struct{} { return t.done }

// Snapshot 是任务状态的只读快照（供 HTTP 等外部消费者使用）。
type Snapshot struct {
	ID       string     `json:"id"`
	Name     string     `json:"name"`
	Status   string     `json:"status"`
	Worker   int        `json:"worker"`
	Runs     int        `json:"runs"`
	ParentID string     `json:"parent_id,omitempty"`
	Err      string     `json:"err,omitempty"`
	Started  *time.Time `json:"started,omitempty"`
	Finished *time.Time `json:"finished,omitempty"`
}

// Snapshot 返回当前状态快照。
func (t *Task) Snapshot() Snapshot {
	t.mu.Lock()
	defer t.mu.Unlock()
	s := Snapshot{
		ID:       t.id,
		Name:     t.name,
		Status:   Status(t.status.Load()).String(),
		Worker:   t.worker,
		Runs:     t.runs,
		Started:  timePtrOrNil(t.started),
		Finished: timePtrOrNil(t.finished),
	}
	if t.parent != nil {
		s.ParentID = t.parent.id
	}
	if t.err != nil {
		s.Err = t.err.Error()
	}
	return s
}

func timePtrOrNil(t time.Time) *time.Time {
	if t.IsZero() {
		return nil
	}
	v := t
	return &v
}

// addChild 在父任务（可能为 nil）下登记未终结子任务。
func (t *Task) addChild(c *Task) {
	if t == nil {
		return
	}
	t.mu.Lock()
	t.children[c] = struct{}{}
	t.mu.Unlock()
}

// removeChild 在子任务终结时从父集合摘除，避免集合无限增长。
func (t *Task) removeChild(c *Task) {
	if t == nil {
		return
	}
	t.mu.Lock()
	delete(t.children, c)
	t.mu.Unlock()
}

// terminalize 是任务进入终态的唯一入口。返回 true 表示本次调用完成了
// Queued/Running -> 终态的迁移；重复调用返回 false（用于幂等）。
//
// fromQueued 为 true 时表示任务从未执行（提交前取消或关闭时清理队列），
// 此时只允许迁移到 Canceled。
func (t *Task) terminalize(now time.Time, st Status, err error, worker int, ran bool, bus *event.Bus) bool {
	t.mu.Lock()
	cur := Status(t.status.Load())
	if cur.Terminal() {
		t.mu.Unlock()
		return false
	}
	if fromQueued := cur == StatusQueued; fromQueued && st != StatusCanceled {
		// 未执行的任务只能以取消终结。
		st = StatusCanceled
		if err == nil {
			err = context.Canceled
		}
	}
	t.status.Store(int32(st))
	t.err = err
	t.finished = now
	if ran {
		t.runs++
	}
	if worker >= 0 && cur == StatusRunning {
		t.worker = worker
	} else if worker >= 0 {
		t.worker = worker
	}
	t.mu.Unlock()
	close(t.done)

	var kind event.Kind
	switch st {
	case StatusCompleted:
		kind = event.KindTaskCompleted
	case StatusCanceled:
		kind = event.KindTaskCanceled
	case StatusFailed:
		kind = event.KindTaskFailed
	}
	parentID := ""
	if t.parent != nil {
		parentID = t.parent.id
	}
	bus.Emit(now, kind, t.id, parentID, worker, err, map[string]any{
		"name": t.name,
		"runs": t.runs,
	})
	if t.parent != nil {
		t.parent.removeChild(t)
	}
	return true
}

// finalizeQueuedCanceled 用于任务已通过 Queued->Canceled CAS、且尚未做
// 终结记账的取消路径（外部 Cancel 与 ShutdownNow）。它完成错误记录、
// done 关闭、事件发布与父子摘除，但不重复做状态迁移。
func (t *Task) finalizeQueuedCanceled(now time.Time, err error, bus *event.Bus) {
	t.mu.Lock()
	if Status(t.status.Load()) != StatusCanceled {
		t.mu.Unlock()
		return
	}
	t.err = err
	t.finished = now
	t.mu.Unlock()
	close(t.done)
	parentID := ""
	if t.parent != nil {
		parentID = t.parent.id
	}
	bus.Emit(now, event.KindTaskCanceled, t.id, parentID, -1, err, map[string]any{
		"name": t.name,
		"runs": t.runs,
	})
	if t.parent != nil {
		t.parent.removeChild(t)
	}
}

// markRunning 将任务从 Queued CAS 为 Running。只有成功的 worker 可以执行。
func (t *Task) markRunning(now time.Time, worker int, bus *event.Bus) bool {
	if !t.status.CompareAndSwap(int32(StatusQueued), int32(StatusRunning)) {
		return false
	}
	t.mu.Lock()
	t.started = now
	t.worker = worker
	t.mu.Unlock()
	bus.Emit(now, event.KindTaskStarted, t.id, parentIDOrEmpty(t), worker, nil,
		map[string]any{"name": t.name})
	return true
}

func parentIDOrEmpty(t *Task) string {
	if t.parent != nil {
		return t.parent.id
	}
	return ""
}

// Context 是传给任务体的操作接口：派生任务、等待任务、检查当前任务。
type Context interface {
	// TaskID 返回正在执行的任务 ID。
	TaskID() string
	// Spawn 派生子任务并放入本 worker 的本地双端队列。
	Spawn(fn Func, opts ...Option) (*Task, error)
	// Wait 等待目标任务终结。为避免“所有 worker 阻塞等待子任务”的
	// 饥饿死锁，等待期间当前 worker 会继续帮助执行可运行任务
	//（help-the-child / helpJoin），而不是睡死在线程上。
	Wait(target *Task) error
}

// taskContext 是任务体内可见的 Context 实现，绑定到执行它的那次执行。
type taskContext struct {
	task   *Task
	ex     *Executor // 执行期注入：派生/帮助执行都依赖它
	worker int       // 当前 worker 编号
}

func (c *taskContext) TaskID() string { return c.task.id }

func (c *taskContext) Spawn(fn Func, opts ...Option) (*Task, error) {
	return c.ex.spawnChild(c.worker, c.task, fn, opts...)
}

func (c *taskContext) Wait(target *Task) error {
	return c.ex.helpWait(c.worker, target)
}

// runBody 执行任务体并把 panic 转为错误。调用方必须已 markRunning。
func runBody(ex *Executor, w *worker, t *Task) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = &PanicError{Value: r, Stack: debug.Stack()}
		}
	}()
	return t.fn(t.ctx, &taskContext{task: t, ex: ex, worker: w.id})
}
