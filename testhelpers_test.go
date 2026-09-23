package agingqueue

import (
	"context"
	"sync"
	"time"
)

// recordExecutor 记录作业开始执行的顺序；可阻塞用于并发槽控制。
type recordExecutor struct {
	mu      sync.Mutex
	order   []string
	started map[string]int
	gate    chan struct{} // 非 nil 时 Execute 阻塞到 gate 关闭或 ctx 取消
}

func newRecordExecutor() *recordExecutor {
	return &recordExecutor{started: make(map[string]int)}
}

func (r *recordExecutor) Execute(ctx context.Context, job *JobHandle) error {
	r.mu.Lock()
	r.order = append(r.order, job.ID)
	r.started[job.ID]++
	r.mu.Unlock()
	if r.gate != nil {
		select {
		case <-r.gate:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}

func (r *recordExecutor) Order() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]string, len(r.order))
	copy(out, r.order)
	return out
}

func (r *recordExecutor) StartedCount(id string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.started[id]
}

// newTestScheduler 构造基于假时钟的调度器并注册记录执行器。
// 默认同步执行：消除执行器 goroutine 的调度抖动，使"派发->完成->重试"
// 在一次 Submit/Advance 调用栈内确定完成。需要阻塞型执行器（gate/
// blocking）的测试应改用 newAsyncTestScheduler。
func newTestScheduler(cfg Config) (*Scheduler, *FakeClock, *recordExecutor) {
	return newSched(cfg, true)
}

// newAsyncTestScheduler 使用异步执行（每作业独立 goroutine），适合
// 占槽/阻塞类执行器的测试。
func newAsyncTestScheduler(cfg Config) (*Scheduler, *FakeClock, *recordExecutor) {
	return newSched(cfg, false)
}

func newSched(cfg Config, syncExec bool) (*Scheduler, *FakeClock, *recordExecutor) {
	clk := NewFakeClock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	if cfg.Clock == nil {
		cfg.Clock = clk
	}
	if cfg.AgingStep == 0 {
		cfg.AgingStep = time.Second
	}
	if cfg.MaxConcurrency == 0 {
		cfg.MaxConcurrency = 1
	}
	if cfg.DefaultMaxAttempts == 0 {
		cfg.DefaultMaxAttempts = 1
	}
	if cfg.DefaultBackoff == 0 {
		cfg.DefaultBackoff = 100 * time.Millisecond
	}
	if cfg.MaxBackoff == 0 {
		cfg.MaxBackoff = time.Second
	}
	cfg.SynchronousExec = syncExec
	rec := newRecordExecutor()
	s := NewScheduler(cfg)
	s.RegisterExecutor("rec", rec)
	s.Start()
	return s, clk, rec
}

// waitFor 轮询条件直到为真或超时（测试里时钟是假的，只能用真实时间轮询）。
func waitFor(timeout time.Duration, fn func() bool) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if fn() {
			return true
		}
		time.Sleep(time.Millisecond)
	}
	return fn()
}

// waitForState 等待作业进入指定状态。
func waitForState(s *Scheduler, id string, st State) bool {
	return waitFor(2*time.Second, func() bool {
		v, err := s.Get(id)
		return err == nil && v.State == st
	})
}

// advance 推进假时钟。FakeClock 的到期回调在 Advance 调用栈内同步
// 内联执行（pump），因此 Advance 返回时本次时间推进的全部状态变更
// （退避到期、老化跳档、派发）都已完成，无需再 sleep 或等待收敛。
func advance(clk *FakeClock, d time.Duration) {
	clk.Advance(d)
}

// waitEvent 等待事件流中出现满足 pred 的事件。
// 先订阅、再查历史：订阅建立后发出的事件不会丢（进入有缓冲通道），
// 订阅之前的事件由紧随其后的历史遍历覆盖，二者合起来无窗口缺口。
func waitEvent(s *Scheduler, timeout time.Duration, pred func(Event) bool) bool {
	log := s.Events()
	ch, _ := log.Subscribe()
	defer log.Unsubscribe(ch)
	for _, e := range log.Events(0, 0) {
		if pred(e) {
			return true
		}
	}
	deadline := time.After(timeout)
	for {
		select {
		case e := <-ch:
			if pred(e) {
				return true
			}
		case <-deadline:
			return false
		}
	}
}

// waitBoostedTo 等待作业老化到指定有效优先级。
func waitBoostedTo(s *Scheduler, id string, pri int) bool {
	return waitEvent(s, 2*time.Second, func(e Event) bool {
		return e.JobID == id && e.Type == EventPriorityBoost && e.Priority == pri
	})
}

// waitReady 等待退避作业回到就绪（EventReady）。
func waitReady(s *Scheduler, id string) bool {
	return waitEvent(s, 2*time.Second, func(e Event) bool {
		return e.JobID == id && e.Type == EventReady
	})
}
