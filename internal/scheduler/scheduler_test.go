package scheduler_test

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"worksteal/internal/clock"
	"worksteal/internal/event"
	"worksteal/internal/executor"
	"worksteal/internal/scheduler"
)

// stubExec 是确定性执行器：提交即内联执行并计数，满足 scheduler.Executor。
type stubExec struct {
	name string
	bus  *event.Bus
	n    atomic.Int64
}

func (s *stubExec) Submit(fn executor.Func, _ ...executor.Option) (*executor.Task, error) {
	if err := fn(context.Background(), nil); err != nil {
		return nil, err
	}
	s.n.Add(1)
	return nil, nil
}
func (s *stubExec) Name() string    { return s.name }
func (s *stubExec) Bus() *event.Bus { return s.bus }

func newStub() *stubExec {
	name := "stub"
	return &stubExec{name: name, bus: event.NewBus(name)}
}

// TestAfterWithFakeClock 延迟任务只在时钟推进到点并 Kick 后提交。
func TestAfterWithFakeClock(t *testing.T) {
	f := clock.NewFake(time.Date(2026, 9, 24, 0, 0, 0, 0, time.UTC))
	stub := newStub()
	s := scheduler.New(stub, f)
	defer s.Shutdown(context.Background())

	var n atomic.Int64
	if _, err := s.After("job", 10*time.Second, func(ctx context.Context, c executor.Context) error {
		n.Add(1)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	f.Advance(9 * time.Second)
	s.Kick()
	if n.Load() != 0 {
		t.Fatalf("fired early: %d", n.Load())
	}
	f.Advance(1 * time.Second)
	waitForKicking(t, s, func() bool { return n.Load() == 1 }, time.Second)
	if s.Pending() != 0 {
		t.Fatalf("one-shot left pending: %d", s.Pending())
	}
}

// TestEveryWithFakeClock 周期任务按假时钟多次触发，取消后不再触发。
func TestEveryWithFakeClock(t *testing.T) {
	f := clock.NewFake(time.Unix(0, 0))
	stub := newStub()
	s := scheduler.New(stub, f)
	defer s.Shutdown(context.Background())

	var n atomic.Int64
	h, err := s.Every("tick", 2*time.Second, func(ctx context.Context, c executor.Context) error {
		n.Add(1)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 5; i++ {
		f.Advance(2 * time.Second)
		waitForKicking(t, s, func() bool { return n.Load() >= int64(i+1) }, time.Second)
	}
	if n.Load() != 5 {
		t.Fatalf("fired %d want 5", n.Load())
	}
	h.Cancel()
	f.Advance(10 * time.Second)
	s.Kick()
	if n.Load() != 5 {
		t.Fatalf("after cancel fired %d, want 5", n.Load())
	}
}

// TestAtAbsolute 绝对时刻触发。
func TestAtAbsolute(t *testing.T) {
	start := time.Unix(1000, 0)
	f := clock.NewFake(start)
	stub := newStub()
	s := scheduler.New(stub, f)
	defer s.Shutdown(context.Background())

	var n atomic.Int64
	if _, err := s.At("abs", start.Add(3*time.Second), func(ctx context.Context, c executor.Context) error {
		n.Add(1)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	f.Advance(3 * time.Second)
	waitForKicking(t, s, func() bool { return n.Load() == 1 }, time.Second)
}

// TestNegativeTreatedImmediate 已过期的时刻立即触发。
func TestNegativeTreatedImmediate(t *testing.T) {
	f := clock.NewFake(time.Unix(0, 0))
	stub := newStub()
	s := scheduler.New(stub, f)
	defer s.Shutdown(context.Background())

	var n atomic.Int64
	if _, err := s.After("late", -time.Second, func(ctx context.Context, c executor.Context) error {
		n.Add(1)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	waitForKicking(t, s, func() bool { return n.Load() == 1 }, time.Second)
}

// TestSchedulerDrivesRealExecutor 端到端：假时钟驱动调度器，真执行器
// （用真实时钟）负责真正跑任务。
func TestSchedulerDrivesRealExecutor(t *testing.T) {
	f := clock.NewFake(time.Unix(0, 0))
	ex := executor.New(executor.Config{Workers: 2, Name: "sched-ex"})
	s := scheduler.New(ex, f)

	var n atomic.Int64
	if _, err := s.After("late", time.Minute, func(ctx context.Context, c executor.Context) error {
		n.Add(1)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	f.Advance(time.Minute)
	s.Kick()
	// 假时钟下后台循环重挂后不会自动再触发；轮询期间持续 Kick 兜底，
	// 给“调度提交 -> 执行器收件箱 -> worker 执行”链路足够机会完成。
	waitForKicking(t, s, func() bool { return n.Load() == 1 }, 2*time.Second)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	s.Shutdown(ctx)
	if err := ex.Shutdown(ctx); err != nil {
		t.Fatal(err)
	}
}

// TestShutdownStopsFiring 调度器关闭后，未到期任务不再提交。
func TestShutdownStopsFiring(t *testing.T) {
	f := clock.NewFake(time.Unix(0, 0))
	stub := newStub()
	s := scheduler.New(stub, f)

	var n atomic.Int64
	_, _ = s.After("future", time.Minute, func(ctx context.Context, c executor.Context) error {
		n.Add(1)
		return nil
	})
	s.Shutdown(context.Background())
	f.Advance(2 * time.Minute)
	s.Kick()
	if n.Load() != 0 {
		t.Fatalf("fired after scheduler shutdown: %d", n.Load())
	}
	// 关闭后再排程应报错。
	if _, err := s.After("x", time.Second, func(context.Context, executor.Context) error { return nil }); err == nil {
		t.Fatal("expected error scheduling after shutdown")
	}
}

// waitForKicking 在等待条件期间周期性 Kick 调度器。
// 假时钟下循环一旦重挂到“未来”定时器便不会自行触发，因此推进后需要
// 反复 Kick 来驱动它；这是 clock.Fake 的确定性用法，不是生产语义。
func waitForKicking(t *testing.T, s *scheduler.Scheduler, cond func() bool, max time.Duration) {
	t.Helper()
	deadline := time.Now().Add(max)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		s.Kick()
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatal("condition not met before deadline")
}
