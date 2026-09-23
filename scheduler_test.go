package agingqueue

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"
)

// TestClockJump_MultipleLevelsAndBackoff 一次大跃迁跨越多个老化档位与
// 退避窗口，验证 pump 把跨越的状态全部补齐，作业按正确顺序执行。
//
// 编排（并发=1）：
//   - HOLD(pri0) 占槽；FLAKY(pri3)、LOW(pri0)、REHOLD(pri1) 排队；
//   - 放行 HOLD -> FLAKY 首跑失败进 5s 退避，REHOLD(1>LOW0) 接管占槽，
//     LOW 始终排队；
//   - 大跃迁 60s：LOW 0->9（9 档），FLAKY 在 5s 退避到期后继续老化到 9；
//   - 跃迁后提交 HOT(pri9)；放行 REHOLD 后，FLAKY、LOW 都已 9，
//     按 FIFO：FLAKY(seq 更老) -> LOW -> HOT。
func TestClockJump_MultipleLevelsAndBackoff(t *testing.T) {
	s, clk, rec := newAsyncTestScheduler(Config{
		MaxConcurrency:     1,
		AgingStep:          time.Second,
		DefaultMaxAttempts: 3,
		DefaultBackoff:     5 * time.Second,
	})
	defer s.Close()

	s.RegisterExecutor("flaky", &failNTimesExecutor{n: 1})
	s.RegisterExecutor("rec", rec)
	gateHold := NewGateExecutor()
	gateRehold := NewGateExecutor()
	s.RegisterExecutor("hold", gateHold)
	s.RegisterExecutor("rehold", gateRehold)

	mk := func(id, typ string, p int) {
		t.Helper()
		if _, err := s.Submit(SubmitOptions{ID: id, Type: typ, Priority: p}); err != nil {
			t.Fatal(err)
		}
	}

	mk("HOLD", "hold", 0)
	if !waitForState(s, "HOLD", StateRunning) {
		t.Fatal("HOLD never started")
	}
	mk("FLAKY", "flaky", 3)
	mk("LOW", "rec", 0)
	// REHOLD 优先级 1：高于 LOW(0)，使 FLAKY 失败后由 REHOLD（而非 LOW）
	// 接管占槽；跃迁后 LOW 老化到 9 会反超 REHOLD，不影响最终派发顺序。
	mk("REHOLD", "rehold", 1)

	gateHold.Release() // HOLD 成功 -> FLAKY 首跑失败退避 -> REHOLD 占槽
	if !waitForState(s, "FLAKY", StateDelayed) {
		t.Fatal("FLAKY did not fail once and enter backoff")
	}
	if !waitForState(s, "REHOLD", StateRunning) {
		t.Fatal("REHOLD did not re-occupy the freed slot")
	}
	if v, _ := s.Get("LOW"); v.State != StateQueued {
		t.Fatalf("LOW state=%s, want queued (slot must stay occupied)", v.State)
	}

	// 大跃迁 60s（覆盖 9 个老化档 + FLAKY 的 5s 退避及后续老化）。
	advance(clk, 60*time.Second)
	mk("HOT", "rec", 9)
	if !waitBoostedTo(s, "LOW", 9) {
		t.Fatal("LOW did not age to 9 across the jump")
	}

	v, _ := s.Get("LOW")
	if v.EffectivePriority != 9 {
		t.Fatalf("LOW effective=%d, want 9 after jump", v.EffectivePriority)
	}
	v, _ = s.Get("FLAKY")
	if v.State != StateQueued || v.EffectivePriority != 9 {
		t.Fatalf("FLAKY state=%s effective=%d, want queued/9", v.State, v.EffectivePriority)
	}

	gateRehold.Release()
	if !waitFor(2*time.Second, func() bool {
		v1, _ := s.Get("LOW")
		v2, _ := s.Get("FLAKY")
		v3, _ := s.Get("HOT")
		return v1.State == StateSucceeded &&
			v2.State == StateSucceeded &&
			v3.State == StateSucceeded
	}) {
		t.Fatalf("jobs did not all succeed after jump: %v", s.Metrics())
	}

	// FLAKY 是 flaky 执行器；LOW/HOT 用 rec。LOW 必须先于 HOT。
	order := rec.Order()
	lowIdx, hotIdx := indexOf(order, "LOW"), indexOf(order, "HOT")
	if lowIdx < 0 || hotIdx < 0 || lowIdx > hotIdx {
		t.Fatalf("aged LOW must precede fresh HOT after jump, order=%v", order)
	}

	// 事件证据：LOW 在跃迁期间获得恰好 9 次跳档（0->9）。
	var boosts int
	for _, e := range s.Events().Events(0, 0) {
		if e.JobID == "LOW" && e.Type == EventPriorityBoost {
			boosts++
		}
	}
	if boosts != 9 {
		t.Fatalf("LOW expected 9 boost events across the jump, got %d", boosts)
	}
}

func indexOf(xs []string, x string) int {
	for i, v := range xs {
		if v == x {
			return i
		}
	}
	return -1
}

// failNTimesExecutor 前 n 次失败。
type failNTimesExecutor struct {
	mu sync.Mutex
	n  int
	k  int
}

func (f *failNTimesExecutor) Execute(_ context.Context, _ *JobHandle) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.k++
	if f.k <= f.n {
		return errors.New("boom")
	}
	return nil
}

// TestDuplicateCancel_Queued 重复取消排队作业：第一次 canceled，之后幂等冲突。
func TestDuplicateCancel_Queued(t *testing.T) {
	b := newBlockingExecutor(1)
	s, _, _ := newAsyncTestScheduler(Config{MaxConcurrency: 1})
	defer s.Close()
	s.RegisterExecutor("block", b)

	if _, err := s.Submit(SubmitOptions{ID: "hold", Type: "block", Priority: 9}); err != nil {
		t.Fatal(err)
	}
	if !waitForState(s, "hold", StateRunning) {
		t.Fatal("hold not running")
	}
	if _, err := s.Submit(SubmitOptions{ID: "V", Type: "rec", Priority: 5}); err != nil {
		t.Fatal(err)
	}
	if !waitForState(s, "V", StateQueued) {
		t.Fatal("V not queued")
	}

	out, err := s.Cancel("V")
	if err != nil || out != "canceled" {
		t.Fatalf("first cancel: outcome=%q err=%v", out, err)
	}
	v, _ := s.Get("V")
	if v.State != StateCanceled {
		t.Fatalf("V state=%s, want canceled", v.State)
	}

	// 重复取消：明确返回 ErrAlreadyCanceled，且不产生新事件。
	evBefore := len(s.Events().Events(0, 0))
	if _, err := s.Cancel("V"); !errors.Is(err, ErrAlreadyCanceled) {
		t.Fatalf("second cancel err=%v, want ErrAlreadyCanceled", err)
	}
	if _, err := s.Cancel("V"); !errors.Is(err, ErrAlreadyCanceled) {
		t.Fatalf("third cancel err=%v, want ErrAlreadyCanceled", err)
	}
	if got := len(s.Events().Events(0, 0)); got != evBefore {
		t.Fatalf("duplicate cancel emitted events: before=%d after=%d", evBefore, got)
	}

	// 取消不存在的作业 / 释放槽后 V 不复活。
	b.release()
	if _, err := s.Cancel("does-not-exist"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("cancel missing: %v", err)
	}
	time.Sleep(20 * time.Millisecond)
	v, _ = s.Get("V")
	if v.State != StateCanceled {
		t.Fatalf("canceled V resurrected: %s", v.State)
	}
}

// slowCancelExecutor 忽略 ctx 取消，始终阻塞直到 release。用于验证
// "运行中作业已被请求取消、但执行器尚未返回"这一窗口内的重复取消语义。
type slowCancelExecutor struct{ gate chan struct{} }

func newSlowCancelExecutor() *slowCancelExecutor {
	return &slowCancelExecutor{gate: make(chan struct{})}
}

func (e *slowCancelExecutor) Execute(context.Context, *JobHandle) error {
	<-e.gate
	return nil
}

func (e *slowCancelExecutor) release() { close(e.gate) }

// TestDuplicateCancel_Running 取消运行中的作业：第一次 -> cancel_requested，
// 执行器返回后落 canceled；期间重复取消返回 already_canceling。
func TestDuplicateCancel_Running(t *testing.T) {
	slow := newSlowCancelExecutor()
	s, clk, _ := newAsyncTestScheduler(Config{MaxConcurrency: 1})
	defer s.Close()
	s.RegisterExecutor("slow", slow)

	if _, err := s.Submit(SubmitOptions{ID: "G", Type: "slow", Priority: 5}); err != nil {
		t.Fatal(err)
	}
	if !waitForState(s, "G", StateRunning) {
		t.Fatal("G not running")
	}

	// 第一次取消 -> cancel_requested（执行器忽略 ctx，状态稳定停留在该窗口）。
	out, err := s.Cancel("G")
	if err != nil || out != "cancel_requested" {
		t.Fatalf("first cancel: %q %v", out, err)
	}
	// 重复取消（执行器尚未返回）-> already_canceling。
	out2, err := s.Cancel("G")
	if err != nil || out2 != "already_canceling" {
		t.Fatalf("duplicate cancel while running: %q %v", out2, err)
	}
	if v, _ := s.Get("G"); v.State != StateCancelRequested {
		t.Fatalf("G state=%s, want cancel_requested", v.State)
	}

	// 执行器返回 -> 终态 canceled。
	slow.release()
	if !waitForState(s, "G", StateCanceled) {
		t.Fatal("G did not settle to canceled")
	}
	if _, err := s.Cancel("G"); !errors.Is(err, ErrAlreadyCanceled) {
		t.Fatalf("cancel after settle: %v", err)
	}
	_ = clk
}

// TestCancel_Delayed 退避中的作业可被取消，立即终态，不再重试。
func TestCancel_Delayed(t *testing.T) {
	fo := &failNTimesExecutor{n: 5}
	s, _, _ := newTestScheduler(Config{
		MaxConcurrency:     1,
		DefaultMaxAttempts: 5,
		DefaultBackoff:     time.Hour,
	})
	defer s.Close()
	s.RegisterExecutor("failn", fo)

	if _, err := s.Submit(SubmitOptions{ID: "D", Type: "failn", Priority: 5}); err != nil {
		t.Fatal(err)
	}
	if !waitForState(s, "D", StateDelayed) {
		t.Fatal("D not delayed")
	}
	out, err := s.Cancel("D")
	if err != nil || out != "canceled" {
		t.Fatalf("cancel delayed: %q %v", out, err)
	}
	m := s.Metrics()
	if m.Delayed != 0 || m.Canceled != 1 {
		t.Fatalf("metrics after delayed cancel: %+v", m)
	}
}

// TestRetriesExhausted 超过最大尝试次数进入 failed 终态，指数退避可验证。
// 单作业 + 事件同步：每次失败进 delayed 堆等待假时钟退避定时器，跃迁越过
// 退避点后通过 EventStarted/EventRetryScheduled 确认下一次尝试。
func TestRetriesExhausted(t *testing.T) {
	alwaysFail := &failNTimesExecutor{n: 1 << 30}
	s, clk, _ := newTestScheduler(Config{
		MaxConcurrency:     1,
		DefaultMaxAttempts: 3,
		DefaultBackoff:     100 * time.Millisecond,
		MaxBackoff:         time.Second,
	})
	defer s.Close()
	s.RegisterExecutor("failn", alwaysFail)

	if _, err := s.Submit(SubmitOptions{ID: "X", Type: "failn", Priority: 5}); err != nil {
		t.Fatal(err)
	}

	// 尝试1 失败 -> 退避 100ms。
	if !waitEvent(s, 2*time.Second, func(e Event) bool {
		return e.JobID == "X" && e.Type == EventRetryScheduled && e.Attempt == 1
	}) {
		t.Fatal("attempt 1 did not fail into backoff")
	}
	advance(clk, 105*time.Millisecond)
	if !waitEvent(s, 2*time.Second, func(e Event) bool {
		return e.JobID == "X" && e.Type == EventStarted && e.Attempt == 2
	}) {
		t.Fatal("attempt 2 did not start")
	}
	if !waitEvent(s, 2*time.Second, func(e Event) bool {
		return e.JobID == "X" && e.Type == EventRetryScheduled && e.Attempt == 2
	}) {
		t.Fatal("attempt 2 did not fail into backoff")
	}
	advance(clk, 205*time.Millisecond)
	if !waitEvent(s, 2*time.Second, func(e Event) bool {
		return e.JobID == "X" && e.Type == EventExhausted
	}) {
		t.Fatal("job did not exhaust retries")
	}
	v, _ := s.Get("X")
	if v.State != StateFailed || v.Attempts != 3 {
		t.Fatalf("X state=%s attempts=%d, want failed/3", v.State, v.Attempts)
	}

	// 事件链完整。
	var types []EventType
	for _, e := range s.Events().Events(0, 0) {
		if e.JobID == "X" {
			types = append(types, e.Type)
		}
	}
	joined := ""
	for _, ty := range types {
		joined += string(ty) + ","
	}
	if !strings.Contains(joined, "submitted,") ||
		!strings.Contains(joined, "retry_scheduled,") ||
		!strings.Contains(joined, "exhausted,") {
		t.Fatalf("event chain missing stages: %s", joined)
	}

	// 退避时长翻倍：两次 retry_scheduled 的 backoff_ms 为 100 与 200。
	var backoffs []int64
	for _, e := range s.Events().Events(0, 0) {
		if e.JobID == "X" && e.Type == EventRetryScheduled {
			if ms, ok := e.Detail["backoff_ms"]; ok {
				backoffs = append(backoffs, ms.(int64))
			}
		}
	}
	if len(backoffs) != 2 || backoffs[0] != 100 || backoffs[1] != 200 {
		t.Fatalf("backoff sequence = %v, want [100 200]", backoffs)
	}

	// 终态作业再取消返回 ErrTerminalCancel。
	if _, err := s.Cancel("X"); !errors.Is(err, ErrTerminalCancel) {
		t.Fatalf("cancel failed job: %v", err)
	}
}

// TestSubmitValidation 入队参数校验与未知类型。
func TestSubmitValidation(t *testing.T) {
	s, _, _ := newTestScheduler(Config{})
	defer s.Close()

	if _, err := s.Submit(SubmitOptions{Type: "rec", Priority: 99}); !errors.Is(err, ErrPriorityRange) {
		t.Fatalf("priority range: %v", err)
	}
	if _, err := s.Submit(SubmitOptions{Type: ""}); !errors.Is(err, ErrEmptyType) {
		t.Fatalf("empty type: %v", err)
	}
	if _, err := s.Submit(SubmitOptions{Type: "unknown"}); !errors.Is(err, ErrUnknownExecutor) {
		t.Fatalf("unknown executor: %v", err)
	}
	v, err := s.Submit(SubmitOptions{ID: "dup", Type: "rec", Priority: 1})
	if err != nil {
		t.Fatal(err)
	}
	_ = v
	if _, err := s.Submit(SubmitOptions{ID: "dup", Type: "rec", Priority: 1}); !errors.Is(err, ErrDuplicateID) {
		t.Fatalf("duplicate id: %v", err)
	}
}

// TestPriorityCapAndTieBreak 老化到顶后稳定在 9，且后来同级高优不能超过更老作业。
func TestPriorityCapAndTieBreak(t *testing.T) {
	s, clk, rec := newAsyncTestScheduler(Config{MaxConcurrency: 1, AgingStep: time.Second})
	defer s.Close()
	b := newBlockingExecutor(1)
	s.RegisterExecutor("block", b)
	if _, err := s.Submit(SubmitOptions{ID: "hold", Type: "block", Priority: 9}); err != nil {
		t.Fatal(err)
	}
	if !waitForState(s, "hold", StateRunning) {
		t.Fatal("hold not running")
	}

	// LOW(0)；跃迁 100s（远超 9 档），封顶 9。
	if _, err := s.Submit(SubmitOptions{ID: "LOW", Type: "rec", Priority: 0}); err != nil {
		t.Fatal(err)
	}
	advance(clk, 100*time.Second)
	time.Sleep(20 * time.Millisecond)
	v, _ := s.Get("LOW")
	if v.EffectivePriority != 9 {
		t.Fatalf("capped effective=%d", v.EffectivePriority)
	}

	// 跃迁后才新来的高优作业：LOW 更老，先跑。
	if _, err := s.Submit(SubmitOptions{ID: "FRESH", Type: "rec", Priority: 9}); err != nil {
		t.Fatal(err)
	}
	b.release()
	if !waitFor(2*time.Second, func() bool {
		v, _ := s.Get("FRESH")
		return v.State == StateSucceeded
	}) {
		t.Fatal("jobs did not drain")
	}
	if order := rec.Order(); len(order) < 2 || order[0] != "LOW" || order[1] != "FRESH" {
		t.Fatalf("tie break order=%v, want LOW,FRESH", order)
	}
}

// TestCancelUnblocksRunningJob_DoesNotLeak 取消后上下文真的被取消。
func TestCancelUnblocksRunningJob_DoesNotLeak(t *testing.T) {
	gate := NewGateExecutor()
	s, _, _ := newAsyncTestScheduler(Config{})
	defer s.Close()
	s.RegisterExecutor("gate", gate)
	if _, err := s.Submit(SubmitOptions{ID: "G", Type: "gate", Priority: 5}); err != nil {
		t.Fatal(err)
	}
	if !waitForState(s, "G", StateRunning) {
		t.Fatal("not running")
	}
	if _, err := s.Cancel("G"); err != nil {
		t.Fatal(err)
	}
	if !waitForState(s, "G", StateCanceled) {
		t.Fatal("not canceled")
	}
	// 调度器应能正常 Close（若 goroutine 泄漏 wg.Wait 会挂死）。
	done := make(chan struct{})
	go func() { _ = s.Close(); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Close hung — likely goroutine/cancel leak")
	}
}

// TestContextCanceledTreatedAsFailureUnlessCanceledJob 执行器返回
// context.Canceled 但作业未被用户取消时，应视为普通可重试失败，
// 重试耗尽后进入 failed（而不是被误判为取消）。
func TestContextCanceledTreatedAsFailureUnlessCanceledJob(t *testing.T) {
	ex := errExecutor{err: context.Canceled}
	s, clk, _ := newTestScheduler(Config{
		MaxConcurrency:     1,
		DefaultMaxAttempts: 2,
		DefaultBackoff:     50 * time.Millisecond,
	})
	defer s.Close()
	s.RegisterExecutor("err", ex)
	if _, err := s.Submit(SubmitOptions{ID: "E", Type: "err", Priority: 5}); err != nil {
		t.Fatal(err)
	}
	if !waitEvent(s, 2*time.Second, func(e Event) bool {
		return e.JobID == "E" && e.Type == EventRetryScheduled
	}) {
		t.Fatal("executor's context.Canceled should be a retryable failure")
	}
	advance(clk, 55*time.Millisecond)
	if !waitEvent(s, 2*time.Second, func(e Event) bool {
		return e.JobID == "E" && e.Type == EventExhausted
	}) {
		t.Fatal("job did not exhaust retries after executor-reported context.Canceled")
	}
	v, _ := s.Get("E")
	if v.State != StateFailed {
		t.Fatalf("E state=%s, want failed (not canceled)", v.State)
	}
}

type errExecutor struct{ err error }

func (e errExecutor) Execute(context.Context, *JobHandle) error { return e.err }
