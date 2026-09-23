package agingqueue

import (
	"context"
	"sync"
	"testing"
	"time"
)

// blockingExecutor 把前 N 次执行挂在 gate 上，之后放行（用于占住并发槽）。
type blockingExecutor struct {
	mu         sync.Mutex
	count      int
	blockFirst int
	gate       chan struct{}
}

func newBlockingExecutor(blockFirst int) *blockingExecutor {
	return &blockingExecutor{gate: make(chan struct{}), blockFirst: blockFirst}
}

func (b *blockingExecutor) Execute(ctx context.Context, _ *JobHandle) error {
	b.mu.Lock()
	b.count++
	n := b.count
	b.mu.Unlock()
	if n <= b.blockFirst {
		select {
		case <-b.gate:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}

func (b *blockingExecutor) release() { close(b.gate) }

// TestAcceptance_LowPriorityRunsUnderSustainedHighPriority
//
// 验收：持续注入高优先级任务，低优先级任务"在规定条件下"必须能运行——
// 条件就是等待老化：当 LOW 老化到与高优先级相同的有效优先级后，
// 它按 FIFO（更老的入队序号）排在新来的高优先级之前。
//
// 这里把条件做成可证伪的：老化前 HIGH 永远先跑；一次时钟跃迁使 LOW
// 满足条件后，即使持续注入 HIGH，LOW 也立刻被执行。
func TestAcceptance_LowPriorityRunsUnderSustainedHighPriority(t *testing.T) {
	s, clk, _ := newAsyncTestScheduler(Config{MaxConcurrency: 1, AgingStep: time.Second, DefaultMaxAttempts: 1})
	defer s.Close()

	blocker := newBlockingExecutor(1) // HIGH-1 占住唯一执行槽
	rec := newRecordExecutor()        // LOW 与后续 HIGH 用它记录顺序
	s.RegisterExecutor("block", blocker)
	s.RegisterExecutor("rec", rec)

	submit := func(id, typ string, pri int) {
		t.Helper()
		if _, err := s.Submit(SubmitOptions{ID: id, Type: typ, Priority: pri}); err != nil {
			t.Fatalf("submit %s: %v", id, err)
		}
	}

	// t=0：HIGH-1（阻塞占槽）、LOW(0) 排队、HIGH-2 排队。
	submit("H1", "block", 9)
	if !waitForState(s, "H1", StateRunning) {
		t.Fatal("H1 never started")
	}
	submit("LOW", "rec", 0)
	submit("H2", "rec", 9)

	// 老化条件未满足（LOW 等待 0s，需要 9 个步长才到 9）。
	advance(clk, 8*time.Second)
	time.Sleep(20 * time.Millisecond)
	if got := rec.StartedCount("LOW"); got != 0 {
		t.Fatalf("LOW ran before aging condition met (started %d times)", got)
	}
	v, _ := s.Get("LOW")
	if v.EffectivePriority != 8 {
		t.Fatalf("LOW effective priority = %d, want 8 after 8s", v.EffectivePriority)
	}

	// 老化条件满足：再跃 1s，LOW 有效优先级到达 9（与新来 HIGH 同级，
	// 但 LOW 入队更早，FIFO 规则使其排在前面）。
	advance(clk, time.Second)
	blocker.release()
	// 在 H1 释放后继续"持续注入"高优先级：H3、H4 立即入队。
	for _, id := range []string{"H3", "H4"} {
		submit(id, "rec", 9)
	}

	if !waitFor(2*time.Second, func() bool { return rec.StartedCount("LOW") == 1 }) {
		t.Fatalf("LOW did not run once aging condition was met: order=%v", rec.Order())
	}
	order := rec.Order()
	if len(order) == 0 || order[0] != "LOW" {
		t.Fatalf("after aging, LOW must dispatch before queued/fresh HIGH jobs, got %v", order)
	}

	// 结构性证据：事件流里必须能看到 LOW 的 9 次跳档。
	var boosts int
	for _, e := range s.Events().Events(0, 0) {
		if e.JobID == "LOW" && e.Type == EventPriorityBoost {
			boosts++
		}
	}
	if boosts != 9 {
		t.Fatalf("LOW expected 9 priority_boosted events, got %d", boosts)
	}
}

// TestAcceptance_FIFOWithinSamePriority 验证相同有效优先级严格 FIFO：
// 两个相同优先级作业按入队顺序执行；且后来的同优先级不能插队。
func TestAcceptance_FIFOWithinSamePriority(t *testing.T) {
	s, clk, rec := newAsyncTestScheduler(Config{MaxConcurrency: 1, AgingStep: 10 * time.Second, DefaultMaxAttempts: 1})
	defer s.Close()

	blocker := newBlockingExecutor(1)
	s.RegisterExecutor("block", blocker)

	submit := func(id string, pri int) {
		t.Helper()
		if _, err := s.Submit(SubmitOptions{ID: id, Type: "rec", Priority: pri}); err != nil {
			t.Fatal(err)
		}
	}

	// blocker 占槽：B-hold 用 block 类型。
	if _, err := s.Submit(SubmitOptions{ID: "hold", Type: "block", Priority: 9}); err != nil {
		t.Fatal(err)
	}
	if !waitForState(s, "hold", StateRunning) {
		t.Fatal("hold never started")
	}

	submit("A", 5)
	clk.Advance(time.Second)
	submit("B", 5) // A、B 基础优先级相同，时钟推进不会让任一跳档（步长 10s）
	clk.Advance(time.Second)
	submit("C", 6) // C 优先级更高，理应在 A/B 之前
	clk.Advance(time.Second)
	submit("D", 5) // 又一个 5

	blocker.release()

	if !waitFor(2*time.Second, func() bool {
		v, _ := s.Get("D")
		return v.State == StateSucceeded
	}) {
		t.Fatalf("jobs did not drain: %v", s.Metrics())
	}
	order := rec.Order()
	want := []string{"C", "A", "B", "D"}
	if len(order) != 4 || order[0] != want[0] {
		t.Fatalf("dispatch order = %v, want %v", order, want)
	}
	// 5 档内部严格 FIFO
	got5 := []string{}
	for _, id := range order {
		if id == "A" || id == "B" || id == "D" {
			got5 = append(got5, id)
		}
	}
	if len(got5) != 3 || got5[0] != "A" || got5[1] != "B" || got5[2] != "D" {
		t.Fatalf("same-priority FIFO violated: %v", got5)
	}
}

// TestAcceptance_RetryKeepsAgeAnchor 验收"重试不允许通过重置年龄长期插队"。
//
// 编排（并发=1，两个独立 gate 精确接力占槽）：
//   - HOLD(pri0) 占槽；RETRY(pri5) 在其后排队，老化 2s：5 -> 7；
//   - REHOLD(pri0) 排队；放行 HOLD -> RETRY 首跑失败进 500ms 退避，
//     REHOLD 接管占槽（pri0 永远低于 RETRY，不与之竞争）；
//   - 跃迁越过退避点再老化 2s：RETRY 保留排队年龄 5+2，再 +2 = 9
//     （500ms 退避不计入）；
//   - 新来 NEW(pri9) 与 RETRY 同为有效优先级 9，RETRY 入队序号更老，
//     按 FIFO 必须先于 NEW 执行，且第二次尝试成功。
//
// 这证明重试既没有重置累计等待（否则到不了 9），也没有换新序号插队。
func TestAcceptance_RetryKeepsAgeAnchor(t *testing.T) {
	fo := &failOnceExecutor{}
	s, clk, rec := newAsyncTestScheduler(Config{
		MaxConcurrency:     1,
		AgingStep:          time.Second,
		DefaultMaxAttempts: 3,
		DefaultBackoff:     500 * time.Millisecond,
	})
	defer s.Close()
	s.RegisterExecutor("failonce", fo)
	gateHold := NewGateExecutor()
	gateRehold := NewGateExecutor()
	s.RegisterExecutor("hold", gateHold)
	s.RegisterExecutor("rehold", gateRehold)

	submit := func(id, typ string, pri int) {
		t.Helper()
		if _, err := s.Submit(SubmitOptions{ID: id, Type: typ, Priority: pri}); err != nil {
			t.Fatal(err)
		}
	}

	// HOLD 占槽；RETRY 排队老化。
	submit("HOLD", "hold", 0)
	if !waitForState(s, "HOLD", StateRunning) {
		t.Fatal("HOLD never started")
	}
	submit("RETRY", "failonce", 5)
	advance(clk, 2*time.Second)
	if !waitBoostedTo(s, "RETRY", 7) {
		t.Fatal("RETRY did not age to 7 while queued behind HOLD")
	}
	v1, _ := s.Get("RETRY")
	anchor, seq := v1.EnqueuedAt, v1.Seq

	// REHOLD 低优先级排队；放行 HOLD，RETRY 首跑失败，REHOLD 接管占槽。
	submit("REHOLD", "rehold", 0)
	gateHold.Release()
	if !waitForState(s, "RETRY", StateDelayed) {
		t.Fatal("RETRY never reached delayed state")
	}
	if !waitForState(s, "REHOLD", StateRunning) {
		t.Fatal("REHOLD did not re-occupy the slot")
	}

	// 越过 500ms 退避点 + 再老化 2s（用余量避免边界）。
	advance(clk, 2505*time.Millisecond)
	if !waitBoostedTo(s, "RETRY", 9) {
		v, _ := s.Get("RETRY")
		t.Fatalf("RETRY did not age back to 9: state=%s eff=%d", v.State, v.EffectivePriority)
	}

	v2, _ := s.Get("RETRY")
	if !v2.EnqueuedAt.Equal(anchor) {
		t.Fatalf("retry reset enqueued_at: before=%v after=%v", anchor, v2.EnqueuedAt)
	}
	if v2.Seq != seq {
		t.Fatalf("retry changed seq: got %d, want %d", v2.Seq, seq)
	}
	if v2.Attempts != 1 {
		t.Fatalf("attempts = %d, want 1 before retry dispatch", v2.Attempts)
	}

	// 新来基础优先级 9 的 NEW：同级，RETRY 更老先跑（第二次成功），NEW 随后。
	submit("NEW", "rec", 9)
	gateRehold.Release()
	if !waitFor(2*time.Second, func() bool {
		v, _ := s.Get("RETRY")
		return v.State == StateSucceeded && v.Attempts == 2
	}) {
		t.Fatal("RETRY did not succeed on 2nd attempt before NEW")
	}
	if !waitForState(s, "NEW", StateSucceeded) {
		t.Fatal("NEW never ran")
	}
	if order := rec.Order(); len(order) != 1 || order[0] != "NEW" {
		t.Fatalf("NEW should execute exactly once and only after RETRY, got %v", order)
	}

	// 事件证据：同一作业经历 retry_scheduled、重试后的 priority_boosted、
	// started(attempt=2)，且自始至终 seq 不变。
	var sawRetry, sawSecondAttempt, sawBoostAfterRetry bool
	for _, e := range s.Events().Events(0, 0) {
		if e.JobID != "RETRY" {
			continue
		}
		switch e.Type {
		case EventRetryScheduled:
			sawRetry = true
		case EventStarted:
			if e.Attempt == 2 {
				sawSecondAttempt = true
			}
		case EventPriorityBoost:
			if sawRetry {
				sawBoostAfterRetry = true
			}
		}
	}
	if !sawRetry || !sawSecondAttempt {
		t.Fatalf("missing retry lifecycle events: retry=%v second=%v", sawRetry, sawSecondAttempt)
	}
	if !sawBoostAfterRetry {
		t.Fatal("retry job never aged after requeue — age anchor was reset")
	}
}

// failOnceExecutor 第一次失败，之后成功。
type failOnceExecutor struct{}

func (failOnceExecutor) Execute(_ context.Context, job *JobHandle) error {
	if job.Attempt == 1 {
		return context.DeadlineExceeded // 任意非 nil 错误即可
	}
	return nil
}
