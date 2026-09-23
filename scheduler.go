package agingqueue

import (
	"container/heap"
	"context"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"sync"
	"time"
)

var (
	// ErrUnknownExecutor 表示提交时指定的类型没有注册执行器。
	ErrUnknownExecutor = errors.New("agingqueue: no executor registered for type")
	// ErrDuplicateID 表示自定义 ID 与已有作业冲突。
	ErrDuplicateID = errors.New("agingqueue: duplicate job id")
	// ErrEmptyType 表示作业类型为空。
	ErrEmptyType = errors.New("agingqueue: job type is required")
	// ErrPriorityRange 表示优先级越界。
	ErrPriorityRange = errors.New("agingqueue: priority out of range")
	// ErrStopped 表示调度器已关闭。
	ErrStopped = errors.New("agingqueue: scheduler stopped")
)

// Config 控制调度器行为。零值字段在 NewScheduler 中填充默认值。
type Config struct {
	Clock Clock // 默认 RealClock

	// AgingStep：就绪等待每满一个步长，有效优先级 +1。
	AgingStep time.Duration
	// MaxConcurrency：同时运行的作业上限。
	MaxConcurrency int
	// DefaultMaxAttempts：入队未指定时的最大尝试次数。
	DefaultMaxAttempts int
	// DefaultBackoff / MaxBackoff：指数退避的初值与上限。
	DefaultBackoff time.Duration
	MaxBackoff     time.Duration
	// AttemptTimeout：单次尝试超时；0 表示不限制。
	// 注意超时由 context（真实时钟）驱动，FakeClock 下不会触发。
	AttemptTimeout time.Duration
	// SynchronousExec 为 true 时，作业在派发调用栈内同步执行，而不是
	// 新开 goroutine。配合 FakeClock 可使"派发->完成->重试"整条链路
	// 在一次 Advance / Submit 内确定完成，适合单元测试。仅应在执行器
	// 不阻塞（或阻塞由 FakeClock.Sleep/ctx 驱动）时启用。
	SynchronousExec bool
	// EventCapacity：内存事件环形容量；<=0 表示不限制。
	EventCapacity int
}

// Scheduler 是带老化的离散优先级作业队列。
type Scheduler struct {
	cfg    Config
	clock  Clock
	events *EventLog

	mu      sync.Mutex
	jobs    map[string]*Job
	ready   readyHeap
	delayed delayedHeap
	boosts  boostHeap

	seq       int64
	running   int
	started   bool
	closing   bool
	executors map[string]Executor

	wg     sync.WaitGroup
	ctx    context.Context
	cancel context.CancelFunc

	// wakeTimer 是自重新武装的唤醒定时器：到期回调 onWake 执行 pump 并
	// 再次 kick。wakeEpoch 用于作废 RealClock 下已排队但被 Stop 的陈旧回调。
	wakeMu    sync.Mutex
	wakeTimer Timer
	wakeEpoch uint64
}

// NewScheduler 构造调度器并填充默认配置。
func NewScheduler(cfg Config) *Scheduler {
	if cfg.Clock == nil {
		cfg.Clock = NewRealClock()
	}
	if cfg.AgingStep <= 0 {
		cfg.AgingStep = time.Second
	}
	if cfg.MaxConcurrency <= 0 {
		cfg.MaxConcurrency = 1
	}
	if cfg.DefaultMaxAttempts <= 0 {
		cfg.DefaultMaxAttempts = 3
	}
	if cfg.DefaultBackoff <= 0 {
		cfg.DefaultBackoff = 100 * time.Millisecond
	}
	if cfg.MaxBackoff <= 0 {
		cfg.MaxBackoff = 5 * time.Second
	}
	if cfg.EventCapacity == 0 {
		cfg.EventCapacity = 10000
	}
	ctx, cancel := context.WithCancel(context.Background())
	s := &Scheduler{
		cfg:       cfg,
		clock:     cfg.Clock,
		events:    NewEventLog(cfg.EventCapacity),
		jobs:      make(map[string]*Job),
		ready:     make(readyHeap, 0),
		delayed:   make(delayedHeap, 0),
		boosts:    make(boostHeap, 0),
		executors: make(map[string]Executor),
		ctx:       ctx,
		cancel:    cancel,
	}
	return s
}

// RegisterExecutor 注册（或覆盖）某类作业的执行器。
func (s *Scheduler) RegisterExecutor(typ string, ex Executor) {
	s.mu.Lock()
	s.executors[typ] = ex
	s.mu.Unlock()
}

// Events 返回结构化事件日志。
func (s *Scheduler) Events() *EventLog { return s.events }

// Start 使调度器开始响应。Start 不阻塞，也不依赖常驻 goroutine：所有
// 时间驱动的推进都由时钟回调触发——RealClock 下由 time.AfterFunc 的
// 后台 goroutine 执行，FakeClock 下在 Advance 的调用栈内联执行。后者
// 保证测试里 Advance 返回时，本次时间推进引发的状态变更已全部完成。
// 重复调用无效。
func (s *Scheduler) Start() {
	s.mu.Lock()
	if s.started {
		s.mu.Unlock()
		return
	}
	s.started = true
	s.mu.Unlock()
	s.pump() // 立即处理提交（在 Start 前入队）的就绪作业
	s.kick()
}

const forever = time.Duration(1<<63 - 1)

// nextWakeLocked 计算距离下一个时间事件（退避到期 / 老化跳档）的时长；
// 无时间事件时返回 forever。调用方须持有 s.mu。
func (s *Scheduler) nextWakeLocked(now time.Time) time.Duration {
	d := forever
	if s.delayed.Len() > 0 {
		d = s.delayed[0].readyAt.Sub(now)
	}
	if s.boosts.Len() > 0 {
		if b := s.boosts[0].at.Sub(now); b < d {
			d = b
		}
	}
	if d < 0 {
		d = 0
	}
	return d
}

// onWake 是时钟到期回调：推进一次队列，再为下一个时间事件重新武装。
// FakeClock 下它在 Advance 调用栈内同步执行。
func (s *Scheduler) onWake(epoch uint64) {
	s.mu.Lock()
	if s.closing || epoch != s.wakeEpoch {
		s.mu.Unlock()
		return
	}
	s.wakeTimer = nil
	s.mu.Unlock()
	s.pump()
	s.kick()
}

// kick 重新计算最近时间事件并武装定时器。没有时间事件时撤销定时器，
// 进入等待，直到 Submit/Cancel/complete 再次 kick。可在锁外调用。
func (s *Scheduler) kick() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.kickLocked()
}

// kickLocked 在持有 s.mu 时重新武装唤醒定时器。
func (s *Scheduler) kickLocked() {
	if s.closing {
		return
	}
	if s.wakeTimer != nil {
		s.wakeTimer.Stop()
		s.wakeTimer = nil
		s.wakeEpoch++
	}
	d := s.nextWakeLocked(s.clock.Now())
	if d <= 0 || d >= forever {
		// pump 是收敛的：调用 kick 前的 pump 已处理完所有到期工作，
		// 因此这里 d 要么是严格的未来正值，要么无时间事件（forever）。
		// d<=0 属防御分支：不再武装，避免在持锁状态触发时钟的到期内联
		// 回调而自锁。
		return
	}
	s.wakeEpoch++
	epoch := s.wakeEpoch
	s.wakeTimer = s.clock.AfterFunc(d, func() { s.onWake(epoch) })
}

// pumpAndKick 在一次状态变更后于锁外调用：先推进队列（可能派发作业），
// 再为下一个未来时间事件武装定时器。未 Start 时两者都不动作。
func (s *Scheduler) pumpAndKick() {
	s.mu.Lock()
	started := s.started
	s.mu.Unlock()
	if !started {
		return
	}
	s.pump()
	s.kick()
}

// pump 执行所有"当前时刻已到期"的队列推进：退避到期 -> 就绪、
// 老化跳档、派发空闲槽位。一次 pump 会循环到无可推进工作为止，
// 因此单次时钟大跃迁也能把跨越的所有档位/退避全部补齐。
func (s *Scheduler) pump() {
	var launched []func()
	var launchedSync []func()
	s.mu.Lock()

	for {
		now := s.clock.Now()
		progressed := false

		// 1) 退避到期的作业回到就绪队列。
		for s.delayed.Len() > 0 && !s.delayed[0].readyAt.After(now) {
			e := heap.Pop(&s.delayed).(*delayedEntry)
			j := e.job
			j.delayedRef = nil
			j.state = StateQueued
			// 用逻辑到期时刻（而非本次 pump 的当前时刻）作为就绪锚点：
			// 时钟大跃迁期间作业可能早在 readyAt 就该就绪，这样它在
			// [readyAt, now] 区间累积的等待老化才不会丢，与真实时钟
			// 准时唤醒的语义一致。
			j.readySince = e.readyAt
			heap.Push(&s.ready, j)
			s.events.emit(now, EventReady, j, map[string]any{
				"wait_accumulated_ms": j.accWait.Milliseconds(),
				"ready_at":            e.readyAt,
			})
			s.scheduleBoostLocked(j, now)
			progressed = true
		}

		// 2) 应用所有已到点的老化跳档（时钟跃迁可能一次跨越多档）。
		// 每个到期条目代表"升一档"：逐档 +1 并逐档发事件，而不是直接
		// 跳到 effectivePriority(now)（那样会丢失中间档位的事件）。
		// maxAllowed 封顶，防止跃迁积压/极端积压下升过当前等待实际允许的档。
		for s.boosts.Len() > 0 && !s.boosts[0].at.After(now) {
			e := heap.Pop(&s.boosts).(*boostEntry)
			j := e.job
			j.boostRef = nil
			// 防御性校验：只有仍在就绪堆中的条目才有效，否则丢弃
			// （作业可能已被派发、取消、重试重排）。
			if j.state != StateQueued || j.heapIdx < 0 {
				progressed = true
				continue
			}
			maxAllowed := j.effectivePriority(now, s.cfg.AgingStep)
			old := j.curPriority
			newPri := old + 1
			if newPri > maxAllowed {
				newPri = maxAllowed
			}
			if newPri > old {
				j.curPriority = newPri
				heap.Fix(&s.ready, j.heapIdx)
				s.events.emit(now, EventPriorityBoost, j, map[string]any{
					"from": old,
					"to":   newPri,
				})
			}
			if j.curPriority < MaxPriority {
				s.scheduleBoostLocked(j, now)
			}
			progressed = true
		}

		// 3) 空闲执行槽 -> 派发队首。
		for s.running < s.cfg.MaxConcurrency && s.ready.Len() > 0 {
			j := heap.Pop(&s.ready).(*Job)
			j.accWait += now.Sub(j.readySince)
			j.readySince = time.Time{}
			// 作业离开就绪堆：移除它排队期间注册的老化定时器。
			if j.boostRef != nil {
				heap.Remove(&s.boosts, j.boostRef.idx)
				j.boostRef = nil
			}
			j.state = StateRunning
			j.attempts++
			if j.startedAt.IsZero() {
				j.startedAt = now
			}
			s.events.emit(now, EventStarted, j, nil)

			ex := s.executors[j.typ]
			ctx, cancel := context.WithCancel(s.ctx)
			j.cancelFn = cancel
			if s.cfg.AttemptTimeout > 0 {
				ctx, cancel = context.WithTimeout(ctx, s.cfg.AttemptTimeout)
				j.cancelFn = cancel
			}
			s.running++

			handle := &JobHandle{
				ID:         j.id,
				Type:       j.typ,
				Priority:   j.priority,
				Attempt:    j.attempts,
				Payload:    j.payload,
				EnqueuedAt: j.enqueuedAt,
			}
			jobID := j.id
			rx, hasResult := ex.(resultExecutor)
			runOnce := func() {
				var res []byte
				var err error
				if hasResult {
					res, err = rx.ExecuteResult(ctx, handle)
				} else {
					err = ex.Execute(ctx, handle)
				}
				s.complete(jobID, res, err)
			}
			if s.cfg.SynchronousExec {
				// 同步模式：锁外立即执行，结束后 complete 重新加锁。
				launchedSync = append(launchedSync, runOnce)
			} else {
				launched = append(launched, runOnce)
			}
			progressed = true
		}

		if !progressed {
			break
		}
	}

	if n := len(launched); n > 0 {
		// 异步模式：计数在锁释放前完成，与 Close 的 Wait 同步。
		s.wg.Add(n)
		s.mu.Unlock()
		for _, fn := range launched {
			go fn()
		}
		return
	}
	if len(launchedSync) > 0 {
		s.mu.Unlock()
		for _, fn := range launchedSync {
			fn() // 同步执行：可能已把作业置为 delayed/succeeded/...
		}
		// 同步执行后重新武装时间事件（如重试退避），并让 onWake 链路继续。
		s.kick()
		return
	}
	s.mu.Unlock()
}

// resultExecutor 是执行器可选实现的增强接口：除错误外还能返回结果数据。
type resultExecutor interface {
	ExecuteResult(ctx context.Context, job *JobHandle) ([]byte, error)
}

// scheduleBoostLocked 安排该就绪作业的下一次老化跳档。
//
// 就绪等待总量 = accWait + (T - readySince)（accWait 只在派发时结算，
// 排队中以 readySince 为锚点）。下一档 p+1 在等待总量首次达到
// (p+1-base)*step 的时刻 T 到达：
//
//	T = readySince + (curPriority+1-base)*step - accWait
//
// 重试带着 accWait 回来时 readySince 被重置为回到就绪的时刻，公式自动
// 保留历史年龄：重试不会重新计时，也不会拿到更新的入队序号。
func (s *Scheduler) scheduleBoostLocked(j *Job, now time.Time) {
	if j.curPriority >= MaxPriority {
		return
	}
	step := s.cfg.AgingStep
	levels := j.curPriority - j.priority + 1
	at := j.readySince.Add(time.Duration(levels)*step - j.accWait)
	e := &boostEntry{at: at, job: j}
	j.boostRef = e
	heap.Push(&s.boosts, e)
	j.nextBoostAt = at
}

func (s *Scheduler) backoffFor(j *Job) time.Duration {
	// 第 k 次尝试失败后：base * 2^(k-1)，封顶 MaxBackoff。
	d := s.cfg.DefaultBackoff
	if j.backoff > 0 {
		d = j.backoff
	}
	for k := 1; k < j.attempts; k++ {
		d *= 2
		if d >= s.cfg.MaxBackoff || d <= 0 {
			return s.cfg.MaxBackoff
		}
	}
	if d > s.cfg.MaxBackoff {
		return s.cfg.MaxBackoff
	}
	return d
}

// complete 在执行结束时被调用，结算一次尝试。异步执行由 goroutine 调用
// （配对 wg.Add/Done）；同步执行在 pump 调用栈内直接调用（不计 wg）。
func (s *Scheduler) complete(jobID string, result []byte, runErr error) {
	if !s.cfg.SynchronousExec {
		defer s.wg.Done()
	}
	s.mu.Lock()
	j := s.jobs[jobID]
	if j == nil {
		s.mu.Unlock()
		return
	}
	now := s.clock.Now()
	if j.cancelFn != nil {
		j.cancelFn()
		j.cancelFn = nil
	}
	s.running--

	switch {
	case j.state == StateCancelRequested:
		// 运行期间收到取消：无论执行器返回什么，终态为 canceled。
		j.state = StateCanceled
		j.finishedAt = now
		j.lastErr = errString(runErr)
		s.events.emit(now, EventCanceled, j, nil)

	case runErr == nil:
		j.state = StateSucceeded
		j.result = result
		j.finishedAt = now
		s.events.emit(now, EventSucceeded, j, map[string]any{"result_bytes": len(result)})

	case j.attempts >= j.maxAttempts:
		j.state = StateFailed
		j.finishedAt = now
		j.lastErr = runErr.Error()
		s.events.emit(now, EventFailed, j, map[string]any{"fatal": true})
		s.events.emit(now, EventExhausted, j, nil)

	default:
		j.lastErr = runErr.Error()
		s.events.emit(now, EventFailed, j, map[string]any{"fatal": false})
		d := s.backoffFor(j)
		j.state = StateDelayed
		// 移除作业排队时注册的老化定时器：退避期间不计就绪年龄，
		// 该条目属于陈旧引用，留着会在错误时刻触发并被丢弃。
		if j.boostRef != nil {
			heap.Remove(&s.boosts, j.boostRef.idx)
			j.boostRef = nil
		}
		readyAt := now.Add(d)
		e := &delayedEntry{readyAt: readyAt, job: j}
		j.delayedRef = e
		heap.Push(&s.delayed, e)
		s.events.emit(now, EventRetryScheduled, j, map[string]any{
			"backoff_ms":   d.Milliseconds(),
			"next_attempt": j.attempts + 1,
			"retry_at":     readyAt,
		})
	}
	s.mu.Unlock()
	if s.cfg.SynchronousExec {
		// 同步执行：已在 pump 调用栈内，由其外层循环继续派发空出的槽位，
		// 并在 pump 返回后统一 kick 武装退避/老化定时器（不能在此重入 pump）。
		return
	}
	// 异步执行：作业结束空出槽位（可能派发下一个）；失败进退避则武装定时器。
	s.pumpAndKick()
}
func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

// Submit 提交新作业。年龄锚点（enqueuedAt/seq）在首次入队时确定且永不改变。
func (s *Scheduler) Submit(opts SubmitOptions) (*JobView, error) {
	if opts.Type == "" {
		return nil, ErrEmptyType
	}
	if opts.Priority < MinPriority || opts.Priority > MaxPriority {
		return nil, fmt.Errorf("%w: got %d, want [%d,%d]", ErrPriorityRange, opts.Priority, MinPriority, MaxPriority)
	}
	s.mu.Lock()
	if s.closing {
		s.mu.Unlock()
		return nil, ErrStopped
	}
	if _, ok := s.executors[opts.Type]; !ok {
		s.mu.Unlock()
		return nil, fmt.Errorf("%w: %q", ErrUnknownExecutor, opts.Type)
	}
	if opts.ID != "" {
		if _, exists := s.jobs[opts.ID]; exists {
			s.mu.Unlock()
			return nil, fmt.Errorf("%w: %q", ErrDuplicateID, opts.ID)
		}
	}
	s.seq++
	now := s.clock.Now()
	maxAtt := opts.MaxAttempts
	if maxAtt <= 0 {
		maxAtt = s.cfg.DefaultMaxAttempts
	}
	id := opts.ID
	if id == "" {
		id = "job-" + strconv.FormatInt(s.seq, 10)
	}
	j := &Job{
		id:          id,
		typ:         opts.Type,
		priority:    opts.Priority,
		payload:     opts.Payload,
		maxAttempts: maxAtt,
		backoff:     opts.Backoff,
		state:       StateQueued,
		enqueuedAt:  now,
		seq:         s.seq,
		readySince:  now,
		heapIdx:     -1,
	}
	j.curPriority = j.priority
	s.jobs[id] = j
	heap.Push(&s.ready, j)
	s.events.emit(now, EventSubmitted, j, map[string]any{"aging_step_ms": s.cfg.AgingStep.Milliseconds()})
	s.scheduleBoostLocked(j, now)
	view := s.viewLocked(j, now)
	s.mu.Unlock()

	// 若有空闲槽立即派发；并（重新）武装未来时间事件定时器。
	s.pumpAndKick()
	return view, nil
}

// Cancel 请求取消作业，返回取消结果：
//   - "canceled"：作业在排队/退避，已立即进入终态；
//   - "cancel_requested"：作业运行中，已向执行器发出停止信号，终态稍后落定；
//   - "already_canceling"：运行中的作业此前已收到取消（重复取消，幂等）。
//
// 对已经是终态的作业重复取消返回错误（ErrAlreadyCanceled / ErrTerminalCancel）。
func (s *Scheduler) Cancel(id string) (string, error) {
	s.mu.Lock()
	j := s.jobs[id]
	if j == nil {
		s.mu.Unlock()
		return "", ErrNotFound
	}
	now := s.clock.Now()
	outcome := ""
	var err error
	needKick := false
	switch j.state {
	case StateQueued:
		if j.boostRef != nil {
			heap.Remove(&s.boosts, j.boostRef.idx)
			j.boostRef = nil
		}
		heap.Remove(&s.ready, j.heapIdx)
		j.heapIdx = -1
		j.state = StateCanceled
		j.finishedAt = now
		s.events.emit(now, EventCancelRequested, j, map[string]any{"when": "queued"})
		s.events.emit(now, EventCanceled, j, nil)
		outcome, needKick = "canceled", true

	case StateDelayed:
		heap.Remove(&s.delayed, j.delayedRef.idx)
		j.delayedRef = nil
		j.state = StateCanceled
		j.finishedAt = now
		s.events.emit(now, EventCancelRequested, j, map[string]any{"when": "delayed"})
		s.events.emit(now, EventCanceled, j, nil)
		outcome, needKick = "canceled", true

	case StateRunning:
		j.state = StateCancelRequested
		if j.cancelFn != nil {
			j.cancelFn()
		}
		s.events.emit(now, EventCancelRequested, j, map[string]any{"when": "running"})
		outcome = "cancel_requested"

	case StateCancelRequested:
		outcome = "already_canceling"

	case StateCanceled:
		err = ErrAlreadyCanceled

	default: // succeeded / failed
		err = ErrTerminalCancel
	}
	s.mu.Unlock()
	if needKick {
		// 取消排队/退避作业可能改变最近时间事件；也可能空出并发槽。
		s.pumpAndKick()
	}
	return outcome, err
}

// Get 返回单个作业快照；不存在返回 (nil, ErrNotFound)。
func (s *Scheduler) Get(id string) (*JobView, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	j := s.jobs[id]
	if j == nil {
		return nil, ErrNotFound
	}
	return s.viewLocked(j, s.clock.Now()), nil
}

// List 返回全部作业快照，按入队序号（= 同优先级 FIFO 顺序）排序。
func (s *Scheduler) List() []JobView {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.clock.Now()
	out := make([]JobView, 0, len(s.jobs))
	for _, j := range s.jobs {
		out = append(out, *s.viewLocked(j, now))
	}
	sort.Slice(out, func(i, k int) bool { return out[i].Seq < out[k].Seq })
	return out
}

// Metrics 是队列当前计数快照。
type Metrics struct {
	Queued          int `json:"queued"`
	Delayed         int `json:"delayed"`
	Running         int `json:"running"`
	CancelRequested int `json:"cancel_requested"`
	Succeeded       int `json:"succeeded"`
	Canceled        int `json:"canceled"`
	Failed          int `json:"failed"`
	Total           int `json:"total"`
}

// Metrics 返回各状态作业数量。
func (s *Scheduler) Metrics() Metrics {
	s.mu.Lock()
	defer s.mu.Unlock()
	var m Metrics
	m.Queued = s.ready.Len()
	m.Delayed = s.delayed.Len()
	m.Total = len(s.jobs)
	for _, j := range s.jobs {
		switch j.state {
		case StateRunning:
			m.Running++
		case StateCancelRequested:
			m.CancelRequested++
		case StateSucceeded:
			m.Succeeded++
		case StateCanceled:
			m.Canceled++
		case StateFailed:
			m.Failed++
		}
	}
	return m
}

// terminateAllLocked：关闭时把未派发作业置为 canceled 并取消运行中的作业。
func (s *Scheduler) terminateAllLocked(now time.Time) {
	for s.ready.Len() > 0 {
		j := heap.Pop(&s.ready).(*Job)
		if j.boostRef != nil {
			heap.Remove(&s.boosts, j.boostRef.idx)
			j.boostRef = nil
		}
		j.state = StateCanceled
		j.finishedAt = now
		s.events.emit(now, EventCanceled, j, map[string]any{"reason": "shutdown"})
	}
	for s.delayed.Len() > 0 {
		e := heap.Pop(&s.delayed).(*delayedEntry)
		j := e.job
		j.delayedRef = nil
		j.state = StateCanceled
		j.finishedAt = now
		s.events.emit(now, EventCanceled, j, map[string]any{"reason": "shutdown"})
	}
	for s.boosts.Len() > 0 {
		heap.Pop(&s.boosts)
	}
	if s.wakeTimer != nil {
		s.wakeTimer.Stop()
		s.wakeTimer = nil
		s.wakeEpoch++
	}
	for _, j := range s.jobs {
		if j.state == StateRunning {
			j.state = StateCancelRequested
			if j.cancelFn != nil {
				j.cancelFn()
			}
		}
	}
}

// Close 停止调度：撤销唤醒定时器、取消所有运行中作业的 context、把未派发
// 作业置为 canceled，并等待在途执行器 goroutine 返回。可重复调用。
// 未 Start 的调度器也可以直接 Close。
func (s *Scheduler) Close() error {
	s.mu.Lock()
	if s.closing {
		s.mu.Unlock()
		s.wg.Wait()
		return nil
	}
	s.closing = true
	s.terminateAllLocked(s.clock.Now())
	s.mu.Unlock()

	s.cancel()
	s.wg.Wait()
	return nil
}
