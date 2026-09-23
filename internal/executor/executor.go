// Package executor 实现固定工作线程数的工作窃取任务执行器。
//
// 设计要点：
//
//   - 每个 worker 持有一个本地双端队列（internal/deque）：任务体用
//     Spawn 派生的子任务压入自己队列的 bottom 端（LIFO，热任务优先）；
//     空闲 worker 从其他 worker 的 top 端 FIFO 窃取。
//   - 外部 Submit 的任务进入轮询分配的“收件箱”队列。
//   - 关键：任务体内 Wait 等待子任务时，worker 不会阻塞在系统线程上，
//     而是以 helpJoin 方式继续执行自己队列以及窃取其他队列中的任务，
//     从而避免“所有 worker 都在等待子任务”的饥饿死锁——即使只有一个
//     worker 也能完成深递归任务树。
//   - 每个任务只可能被一个 worker 用 Queued->Running 的 CAS 认领，
//     因而任务体至多执行一次。
//   - 所有状态变更都发布结构化事件到 internal/event.Bus。
package executor

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"

	"worksteal/internal/clock"
	"worksteal/internal/deque"
	"worksteal/internal/event"
)

// Stats 是执行器的计数快照。
type Stats struct {
	Submitted   int64 `json:"submitted"`
	Started     int64 `json:"started"`
	Completed   int64 `json:"completed"`
	Failed      int64 `json:"failed"`
	Canceled    int64 `json:"canceled"`
	Steals      int64 `json:"steals"`
	Spawned     int64 `json:"spawned"`
	InFlight    int64 `json:"in_flight"` // 已提交未终结
	Runnable    int   `json:"runnable"`  // 各本地队列近似长度之和
	InInbox     int   `json:"in_inbox"`  // 收件箱中尚未取出的任务数
	IdleWorkers int   `json:"idle_workers"`
}

// Config 配置执行器。
type Config struct {
	// Workers 是固定 worker 数。<1 时取 runtime.NumCPU。
	Workers int
	// Name 出现在事件里，默认自动生成。
	Name string
	// DequeLogSize 是每个本地双端队列的初始容量对数（2^n），默认 8。
	DequeLogSize uint
	// InboxPerWorker 是每个收件箱缓冲大小，默认 1024。
	InboxPerWorker int
	// Bus 注入外部事件总线；为 nil 时内部创建。
	Bus   *event.Bus
	Clock clock.Clock
	// DequeKind 选择双端队列实现："chaselev"（默认）或 "mutex"。
	DequeKind string
}

// Executor 是工作窃取执行器。
type Executor struct {
	cfg     Config
	clock   clock.Clock
	bus     *event.Bus
	workers []*worker

	rootCtx    context.Context
	rootCancel context.CancelCauseFunc

	// 注册表：仅保留未终结任务。
	regMu sync.RWMutex
	reg   map[string]*Task

	// 活跃计数：已提交未终结任务数。
	inFlight sync.WaitGroup

	// 收件箱轮询起点。
	rr atomic.Uint64

	// 关闭：closed 表示停止接收新任务（优雅/立即都立即置位）；
	// stopCh 关闭表示 worker 应当退出（立即模式马上关，优雅模式排空后关）。
	// immediate 表示立即模式：worker 一旦手里没有正在执行的任务就退出，
	// 不再取队列/收件箱中的新任务。
	stopMu       sync.Mutex
	closed       atomic.Bool
	immediate    atomic.Bool
	shutdownGate sync.Mutex // 关闭屏障：取任务到认领期间短暂持有
	stopCh       chan struct{}
	wg           sync.WaitGroup // worker goroutine

	submitted atomic.Int64
	started   atomic.Int64
	completed atomic.Int64
	failed    atomic.Int64
	canceled  atomic.Int64
	steals    atomic.Int64
	spawned   atomic.Int64
}

type worker struct {
	id    int
	ex    *Executor
	q     deque.Deque[Task]
	inbox chan *Task

	// 停车/唤醒（守卫式挂起，无丢失唤醒、无超时轮询）：
	//   - 每次停车创建一个全新的 wake 通道（curWake），唤醒方向它发送信号；
	//     用全新通道保证上一轮残留在通道里的陈旧唤醒不会被本轮误消费；
	//   - sleepMu 串行化“换新通道并标记睡眠后复查队列”与“入队后唤醒”，
	//     杜绝丢失信号。
	sleepMu  sync.Mutex
	sleeping bool
	curWake  chan struct{}
}

// parkUntil 把 worker 挂起，直到被唤醒、关闭，或（若 done 非 nil）
// done 关闭。
//
// 关键不变量（守卫式挂起，无丢失唤醒、无超时轮询）：
//  1. 持 sleepMu 标记 sleeping=true 后、释放锁去等待前，先在锁内复查
//     findWork 与 done；入队方先入队、再向当前 wake 通道发信号（也取
//     sleepMu）。因此要么复查时看到任务/目标完成而不睡，要么唤醒必然
//     送达——不存在“查空后、入睡前”的信号丢失窗口。
//  2. 每次等待都新建 wake 通道，上一轮残留在旧通道里的陈旧唤醒不会被
//     本轮误消费。
//  3. select 直接监听 done 与 stopCh（本函数运行在 worker 自己的
//     goroutine 上，可直接多路等待），不使用一次性桥接协程，因此被提前
//     唤醒后循环回去重等，也不会错过“done 已关闭”这一事实。
func (ex *Executor) parkUntil(w *worker, done <-chan struct{}) (pending *Task, victim int) {
	w.sleepMu.Lock()
	w.sleeping = true
	ex.bus.Emit(ex.clock.Now(), event.KindWorkerParked, "", "", w.id, nil, nil)
	for {
		if ex.halting() || isChanClosed(done) {
			w.sleeping = false
			w.curWake = nil
			w.sleepMu.Unlock()
			ex.bus.Emit(ex.clock.Now(), event.KindWorkerWoken, "", "", w.id, nil, nil)
			return nil, -1
		}
		if t, v := ex.findWork(w); t != nil {
			w.sleeping = false
			w.curWake = nil
			w.sleepMu.Unlock()
			return t, v
		}
		wake := make(chan struct{}, 1)
		w.curWake = wake
		w.sleepMu.Unlock()
		select {
		case <-wake:
		case <-done:
		case <-ex.stopCh:
		}
		w.sleepMu.Lock()
	}
}

// isChanClosed 非阻塞地报告只读通道是否已关闭（nil 通道视为未关闭）。
func isChanClosed(ch <-chan struct{}) bool {
	if ch == nil {
		return false
	}
	select {
	case <-ch:
		return true
	default:
		return false
	}
}

// wakeIfSleeping 仅当 worker 在睡时向其当前唤醒通道投递信号并返回 true；
// 幂等（通道容量 1）。
func (w *worker) wakeIfSleeping() bool {
	w.sleepMu.Lock()
	if !w.sleeping || w.curWake == nil {
		w.sleepMu.Unlock()
		return false
	}
	wake := w.curWake
	w.sleepMu.Unlock()
	select {
	case wake <- struct{}{}:
	default: // 已有待处理的唤醒信号。
	}
	return true
}

// isSleeping 报告该 worker 是否处于停车状态。
func (w *worker) isSleeping() bool {
	w.sleepMu.Lock()
	defer w.sleepMu.Unlock()
	return w.sleeping
}

// New 创建执行器并立即启动固定数量的 worker goroutine。
func New(cfg Config) *Executor {
	if cfg.Workers < 1 {
		cfg.Workers = defaultWorkers()
	}
	if cfg.Name == "" {
		cfg.Name = randomID("ex_", 4)
	}
	if cfg.DequeLogSize == 0 {
		cfg.DequeLogSize = 8
	}
	if cfg.InboxPerWorker < 1 {
		cfg.InboxPerWorker = 1024
	}
	if cfg.Clock == nil {
		cfg.Clock = clock.NewReal()
	}
	if cfg.Bus == nil {
		cfg.Bus = event.NewBus(cfg.Name)
	}
	rootCtx, rootCancel := context.WithCancelCause(context.Background())
	ex := &Executor{
		cfg:        cfg,
		clock:      cfg.Clock,
		bus:        cfg.Bus,
		workers:    make([]*worker, cfg.Workers),
		rootCtx:    rootCtx,
		rootCancel: rootCancel,
		reg:        make(map[string]*Task),
		stopCh:     make(chan struct{}),
	}
	for i := 0; i < cfg.Workers; i++ {
		w := &worker{
			id:    i,
			ex:    ex,
			inbox: make(chan *Task, cfg.InboxPerWorker),
		}
		switch cfg.DequeKind {
		case "mutex":
			w.q = deque.NewMutex[Task](1 << cfg.DequeLogSize)
		default:
			w.q = deque.NewChaseLev[Task](cfg.DequeLogSize)
		}
		ex.workers[i] = w
	}
	for _, w := range ex.workers {
		ex.wg.Add(1)
		go ex.workerLoop(w)
	}
	ex.bus.Emit(ex.clock.Now(), event.KindExecutorStarted, "", "", -1, nil,
		map[string]any{"workers": cfg.Workers, "deque": ex.dequeKind()})
	return ex
}

func (ex *Executor) dequeKind() string {
	if ex.cfg.DequeKind == "mutex" {
		return "mutex"
	}
	return "chaselev"
}

// Name 返回执行器名称。
func (ex *Executor) Name() string { return ex.cfg.Name }

// Bus 返回事件总线（供订阅/挂接 Sink）。
func (ex *Executor) Bus() *event.Bus { return ex.bus }

// Workers 返回固定 worker 数。
func (ex *Executor) Workers() int { return len(ex.workers) }

func (ex *Executor) isStopped() bool {
	return ex.closed.Load()
}

// halting 报告 worker 是否应当立即退出：stopCh 关闭（立即关闭，或优雅
// 排空完成后的收尾）。注意与 isStopped 区分——优雅关闭先置 closed（停止
// 接单）但 worker 仍须继续排空，直到 stopCh 关闭。
func (ex *Executor) halting() bool {
	select {
	case <-ex.stopCh:
		return true
	default:
		return false
	}
}

// Submit 提交一个外部任务，返回任务句柄。执行器关闭后返回 ErrExecutorStopped。
func (ex *Executor) Submit(fn Func, opts ...Option) (*Task, error) {
	if ex.isStopped() {
		return nil, ErrExecutorStopped
	}
	t, err := newTask(ex.rootCtx, fn, nil, opts...)
	if err != nil {
		return nil, err
	}
	ex.regMu.Lock()
	if _, dup := ex.reg[t.id]; dup {
		ex.regMu.Unlock()
		return nil, errors.New("duplicate task id: " + t.id)
	}
	ex.inFlight.Add(1)
	ex.reg[t.id] = t
	ex.regMu.Unlock()
	ex.submitted.Add(1)

	// 轮询选择收件箱；通道满时循环尝试其他收件箱，全部满则阻塞
	//（提供背压）并响应关闭。
	start := int(ex.rr.Add(1)-1) % len(ex.workers)
	if !ex.enqueueInbox(t, start) {
		ex.rollbackSubmit(t)
		return nil, ErrExecutorStopped
	}
	ex.bus.Emit(ex.clock.Now(), event.KindTaskSubmitted, t.id, "", -1, nil,
		map[string]any{"name": t.name})
	ex.wakeOne()
	return t, nil
}

// rollbackSubmit 回收提交失败（执行器并发关闭）时的登记。
func (ex *Executor) rollbackSubmit(t *Task) {
	ex.regMu.Lock()
	delete(ex.reg, t.id)
	ex.regMu.Unlock()
	// 任务从未入队，从 Queued 终结为取消状态以关闭 done 并释放计数。
	t.terminalize(ex.clock.Now(), StatusCanceled, ErrExecutorStopped, -1, false, ex.bus)
	ex.canceled.Add(1)
	ex.inFlight.Done()
}

// enqueueInbox 从 start 起尝试所有收件箱；全部满时阻塞等待空位，
// 期间响应执行器关闭。返回 false 表示因关闭放弃。
func (ex *Executor) enqueueInbox(t *Task, start int) bool {
	n := len(ex.workers)
	for {
		for i := 0; i < n; i++ {
			w := ex.workers[(start+i)%n]
			select {
			case w.inbox <- t:
				return true
			default:
			}
		}
		// 全部满：在起始收件箱上阻塞发送（worker 持续消费必然腾出空位），
		// 同时响应关闭。
		select {
		case ex.workers[start].inbox <- t:
			return true
		case <-ex.stopCh:
			return false
		}
	}
}

// spawnChild 在 worker wid 的本地队列压入子任务（任务体内调用）。
func (ex *Executor) spawnChild(wid int, parent *Task, fn Func, opts ...Option) (*Task, error) {
	if ex.isStopped() {
		return nil, ErrExecutorStopped
	}
	t, err := newTask(parent.ctx, fn, parent, opts...)
	if err != nil {
		return nil, err
	}
	ex.regMu.Lock()
	if _, dup := ex.reg[t.id]; dup {
		ex.regMu.Unlock()
		return nil, errors.New("duplicate task id: " + t.id)
	}
	ex.inFlight.Add(1)
	ex.reg[t.id] = t
	ex.regMu.Unlock()
	parent.addChild(t)
	ex.spawned.Add(1)

	w := ex.workers[wid]
	w.q.PushBottom(t)
	ex.bus.Emit(ex.clock.Now(), event.KindTaskSpawned, t.id, parent.id, wid, nil,
		map[string]any{"name": t.name})
	ex.wakeOne()
	return t, nil
}

// Get 返回已注册任务（未终结）；终结后可能已被摘除，此时返回 nil,false。
func (ex *Executor) Get(id string) (*Task, bool) {
	ex.regMu.RLock()
	t, ok := ex.reg[id]
	ex.regMu.RUnlock()
	return t, ok
}

// Wait 阻塞直到所有已提交任务终结，或 ctx 超时/取消。
func (ex *Executor) Wait(ctx context.Context) error {
	done := make(chan struct{})
	go func() {
		ex.inFlight.Wait()
		close(done)
	}()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// ---------- 取消 ----------

// Cancel 请求取消一个任务：
//   - 排队中：立即终结为 canceled（弹出时跳过，绝不执行）；
//   - 运行中：取消其 context，并递归取消它尚未终结的派生任务。
//
// 任务不存在或已终结时返回 false。
func (ex *Executor) Cancel(id string, reason error) bool {
	t, ok := ex.Get(id)
	if !ok {
		return false
	}
	return ex.cancelTree(t, reason)
}

func (ex *Executor) cancelTree(t *Task, reason error) bool {
	if reason == nil {
		reason = context.Canceled
	}
	cur := t.Status()
	if cur.Terminal() {
		return false
	}
	// 先递归取消子孙：子 ctx 派生于父 ctx，父取消本身也会级联取消 ctx，
	// 但排队中的子任务需要显式终结，否则要等弹出才会发现。
	t.mu.Lock()
	kids := make([]*Task, 0, len(t.children))
	for c := range t.children {
		kids = append(kids, c)
	}
	t.mu.Unlock()
	for _, c := range kids {
		ex.cancelTree(c, reason)
	}

	if cur == StatusQueued {
		if t.status.CompareAndSwap(int32(StatusQueued), int32(StatusCanceled)) {
			t.cancel(reason)
			t.finalizeQueuedCanceled(ex.clock.Now(), reason, ex.bus)
			ex.canceled.Add(1)
			ex.afterTerminal(t)
			return true
		}
		return false
	}
	// Running：只发取消信号，由执行它的 worker 终结。
	t.cancel(reason)
	return true
}

// afterTerminal 处理终结后的记账（注册表摘除 + inFlight 释放）。
// 排队即取消的路径与正常执行终结路径都汇聚到这里。
func (ex *Executor) afterTerminal(t *Task) {
	ex.regMu.Lock()
	delete(ex.reg, t.id)
	ex.regMu.Unlock()
	ex.inFlight.Done()
}

// ---------- worker 主循环 ----------

func (ex *Executor) workerLoop(w *worker) {
	defer ex.wg.Done()
	for {
		t, victim := ex.findWork(w)
		if t != nil {
			ex.execute(w, t, victim)
			continue
		}
		if ex.halting() {
			return
		}
		// 无任务：进入睡眠。注意不能因为 closed（优雅关闭、停止接单）就退出——
		// 此时仍须排空已入队任务；只有 stopCh 关闭（halting）才退出。
		// parkUntil 在持锁标记睡眠后再复查队列，与入队唤醒串行化，杜绝丢失唤醒。
		if t, victim := ex.parkUntil(w, nil); t != nil {
			ex.execute(w, t, victim)
		}
	}
}

// findWork 按“本地 bottom -> 窃取他人 top -> 本地收件箱”的顺序找任务。
// 返回 victim>=0 表示任务来自窃取。
//
// 调用方在认领任务（markRunning 或取消终结）的整个窗口内持有
// shutdownGate，与 ShutdownNow 互斥，杜绝“弹出后、认领前遇到立即关闭”
// 导致任务被执行或漏掉终结的竞态。
func (ex *Executor) findWork(w *worker) (*Task, int) {
	// 立即关闭：不再弹出任何新任务（手里正在执行的除外）。
	if ex.immediate.Load() {
		return nil, -1
	}
	// 1. 本地队列 bottom（LIFO）。
	if t, ok := w.q.PopBottom(); ok {
		return t, -1
	}
	// 2. 从其他 worker 的 top 窃取（FIFO）。
	n := len(ex.workers)
	start := int(fastrand() % uint32(n))
	for i := 1; i < n; i++ {
		v := ex.workers[(start+i)%n]
		if t, ok := v.q.Steal(); ok {
			return t, v.id
		}
	}
	// 3. 自己的收件箱。
	select {
	case t := <-w.inbox:
		return t, -1
	default:
	}
	// 4. 顺手看看别人的收件箱（轮询，非阻塞）。
	for i := 1; i < n; i++ {
		v := ex.workers[(start+i)%n]
		select {
		case t := <-v.inbox:
			return t, -1
		default:
		}
	}
	return nil, -1
}

// claim 持有 shutdownGate 认领任务：立即关闭期间弹出的任务必须取消而不是
// 执行；否则 markRunning。返回 true 表示已成功认领、应当执行。
func (ex *Executor) claim(w *worker, t *Task, victim int) bool {
	ex.shutdownGate.Lock()
	defer ex.shutdownGate.Unlock()
	now := ex.clock.Now()
	if ex.immediate.Load() && t.Status() == StatusQueued {
		if t.status.CompareAndSwap(int32(StatusQueued), int32(StatusCanceled)) {
			t.cancel(ErrExecutorStopped)
			t.finalizeQueuedCanceled(now, ErrExecutorStopped, ex.bus)
			ex.canceled.Add(1)
			ex.afterTerminal(t)
		}
		return false
	}
	if victim >= 0 {
		ex.steals.Add(1)
		ex.bus.Emit(now, event.KindTaskStole, t.id, parentIDOrEmpty(t), w.id, nil,
			map[string]any{"from": victim})
	}
	if !t.markRunning(now, w.id, ex.bus) {
		// 任务在排队期间被取消（或已终结），跳过——任务体一次也不会执行。
		return false
	}
	ex.started.Add(1)
	return true
}

// execute 认领并运行一个被弹出的任务，victim>=0 时记录窃取事件。
func (ex *Executor) execute(w *worker, t *Task, victim int) {
	if !ex.claim(w, t, victim) {
		return
	}
	err := runBody(ex, w, t)
	ex.finish(w, t, err)
}

// finish 决定终态并做记账。
func (ex *Executor) finish(w *worker, t *Task, err error) {
	now := ex.clock.Now()
	cause := context.Cause(t.ctx)
	st := StatusCompleted
	switch {
	case cause != nil:
		// 任务 ctx 被取消（外部 Cancel / ShutdownNow / 父级取消）。
		// 无论任务体返回什么错误（ctx.Err、cause 本身或包装错误），
		// 统一归类为 canceled。
		st = StatusCanceled
		if err == nil {
			err = cause
		}
	case err != nil:
		// ctx 未取消却返回错误：真正的任务失败（含 panic）。
		st = StatusFailed
	}
	if t.terminalize(now, st, err, w.id, true, ex.bus) {
		switch st {
		case StatusCompleted:
			ex.completed.Add(1)
		case StatusFailed:
			ex.failed.Add(1)
		case StatusCanceled:
			ex.canceled.Add(1)
		}
		ex.afterTerminal(t)
	}
	// 任务完成可能唤醒等待它的父任务；helpWait 在 done 上自旋检查，
	// 同时也唤醒一个空闲 worker 防止外部任务无人取。
	ex.wakeOne()
}

// ---------- help-the-child ----------

// helpWait 是防饥饿死锁的核心：等待 target 期间，当前 worker 继续执行
// 自己队列及窃取来的任务（helpJoin），而不是阻塞底层线程。
//
// 单 worker 场景下：父任务调用 Wait(子) 后亲自从本地队列弹出子任务执行，
// 因此任意深度的递归树都能完成，不可能出现“全员等子任务”的死锁。
func (ex *Executor) helpWait(wid int, target *Task) error {
	w := ex.workers[wid]
	// helpJoin 的唤醒通道：任一 worker 终结任务、入队新任务或关闭时都会
	// 尝试唤醒等待中的 worker。用一个独立 watcher 把“target 终结”也桥接成
	// 唤醒，避免等待中的 worker 永久停车（它等待的树可能仍在派生）。
	for {
		// 直接以 done 通道是否关闭判定终结——不能用 Err()!=nil 判定，
		// 因为任务成功完成时 Err() 为 nil（nil 同时表示“未终结”）。
		select {
		case <-target.done:
			return target.Err()
		default:
		}
		if ex.halting() {
			return ErrExecutorStopped
		}
		// 帮助执行：优先本地，其次窃取。
		if t, victim := ex.findWork(w); t != nil {
			ex.execute(w, t, victim)
			continue
		}
		// 无任务可帮忙：可中断地停车等待，直到被新任务/任务终结/关闭唤醒。
		// parkUntil 在睡眠锁内复查时可能已弹出一个任务，必须执行，
		// 否则该任务会被“弹出却无人处理”而泄漏（inFlight 永不归零）。
		if t, victim := ex.parkUntil(w, target.done); t != nil {
			ex.execute(w, t, victim)
		}
	}
}

// ---------- 停车/唤醒（守卫式挂起，无丢失唤醒、无超时轮询） ----------

// wakeOne 唤醒一个当前在睡的 worker（如有）。
func (ex *Executor) wakeOne() {
	for _, w := range ex.workers {
		if w.wakeIfSleeping() {
			return
		}
	}
}

// wakeAll 唤醒所有在睡 worker（关闭时使用）。
func (ex *Executor) wakeAll() {
	for _, w := range ex.workers {
		w.wakeIfSleeping()
	}
}

// ---------- 关闭 ----------

// Shutdown 优雅关闭：立即停止接收新任务，等待所有已提交任务终结后
// 再让 worker 退出。ctx 仅约束等待阶段；返回时 worker 已全部退出。
func (ex *Executor) Shutdown(ctx context.Context) error {
	ex.beginShutdown(false, nil)
	waitCh := make(chan struct{})
	go func() {
		ex.inFlight.Wait()
		close(waitCh)
	}()
	var waitErr error
	select {
	case <-waitCh:
	case <-ctx.Done():
		waitErr = ctx.Err()
	}
	// 所有活跃任务终结（或超时）后，停掉 worker。
	ex.signalWorkersStop()
	ex.wg.Wait()
	ex.bus.Emit(ex.clock.Now(), event.KindExecutorStopped, "", "", -1, nil,
		map[string]any{"graceful": true, "wait_err": errString(waitErr)})
	return waitErr
}

// ShutdownNow 立即关闭：
//  1. 置立即标志、关闭全部任务 ctx、关闭 stopCh——worker 完成手头任务后
//     不再取任何新任务并退出；
//  2. 等待 worker 全部退出；
//  3. 扫描残留：所有没来得及运行的排队任务标记为 canceled。
//
// 返回关闭瞬间在册的任务快照（运行中的为取消中/已响应，排队的为已取消）。
func (ex *Executor) ShutdownNow() []Snapshot {
	ex.beginShutdown(true, ErrExecutorStopped)
	// 持有屏障锁翻转立即标志：要么 worker 在我们之前完成认领（任务正常
	// 运行并响应 ctx 取消），要么它在 claim 里看到 immediate 而取消任务；
	// 不存在“弹出后绕过认领检查去执行”的窗口。
	ex.shutdownGate.Lock()
	ex.immediate.Store(true)
	ex.signalWorkersStop()
	ex.shutdownGate.Unlock()
	// 唤醒可能停在 helpWait/停车点的 worker，使它们尽快退出。
	ex.wakeAll()
	// 等待运行中的任务响应 ctx 取消、worker 退出。
	ex.wg.Wait()

	// worker 已全部退出，注册表不再变化，安全扫描残留排队任务。
	ex.regMu.Lock()
	pending := make([]*Task, 0, len(ex.reg))
	for _, t := range ex.reg {
		pending = append(pending, t)
	}
	ex.regMu.Unlock()
	snaps := make([]Snapshot, 0, len(pending))
	now := ex.clock.Now()
	for _, t := range pending {
		if t.Status() == StatusQueued {
			if t.status.CompareAndSwap(int32(StatusQueued), int32(StatusCanceled)) {
				t.cancel(ErrExecutorStopped)
				t.finalizeQueuedCanceled(now, ErrExecutorStopped, ex.bus)
				ex.canceled.Add(1)
				ex.afterTerminal(t)
			}
		}
		snaps = append(snaps, t.Snapshot())
	}
	ex.bus.Emit(ex.clock.Now(), event.KindExecutorStopped, "", "", -1, nil,
		map[string]any{"graceful": false, "pending_at_call": len(snaps)})
	return snaps
}

// signalWorkersStop 关闭 stopCh（幂等），通知所有 worker 退出。
func (ex *Executor) signalWorkersStop() {
	ex.stopMu.Lock()
	select {
	case <-ex.stopCh:
	default:
		close(ex.stopCh)
	}
	ex.stopMu.Unlock()
}

// beginShutdown 标记执行器不再接收新任务，必要时取消全部任务上下文，
// 并唤醒所有停车中的 worker（优雅模式继续排空，立即模式准备退出）。
func (ex *Executor) beginShutdown(immediate bool, cause error) {
	ex.closed.Store(true)
	ex.bus.Emit(ex.clock.Now(), event.KindExecutorStopping, "", "", -1, nil,
		map[string]any{"immediate": immediate})
	if immediate {
		ex.rootCancel(cause)
	}
	// 唤醒所有停车 worker：优雅模式排空残留后退出，立即模式直接退出。
	ex.wakeAll()
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

// ---------- 统计 ----------

// Stats 返回计数与队列近似长度快照。
func (ex *Executor) Stats() Stats {
	runnable := 0
	inbox := 0
	idle := 0
	for _, w := range ex.workers {
		runnable += w.q.Len()
		inbox += len(w.inbox)
		if w.isSleeping() {
			idle++
		}
	}
	return Stats{
		Submitted:   ex.submitted.Load(),
		Started:     ex.started.Load(),
		Completed:   ex.completed.Load(),
		Failed:      ex.failed.Load(),
		Canceled:    ex.canceled.Load(),
		Steals:      ex.steals.Load(),
		Spawned:     ex.spawned.Load(),
		Runnable:    runnable,
		InInbox:     inbox,
		IdleWorkers: idle,
	}
}

// Snapshots 返回所有未终结任务的快照。
func (ex *Executor) Snapshots() []Snapshot {
	ex.regMu.RLock()
	out := make([]Snapshot, 0, len(ex.reg))
	for _, t := range ex.reg {
		out = append(out, t.Snapshot())
	}
	ex.regMu.RUnlock()
	return out
}
