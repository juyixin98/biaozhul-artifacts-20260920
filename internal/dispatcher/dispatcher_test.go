package dispatcher

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"resourcebooking/internal/clock"
	"resourcebooking/internal/executor"
	"resourcebooking/internal/scheduler"
)

func TestRunnerStartsCompletesAndFails(t *testing.T) {
	epoch := time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC)
	fc := clock.NewFake(epoch)
	sch := scheduler.New(scheduler.NewEventLog(fc))
	// 容量 2：a 与 bad 在 [5,10) 可并存，从而能单独观察 bad
	// 执行失败后的容量释放。
	if _, err := sch.AddResource(scheduler.Resource{ID: "r", Capacity: []int64{2}}); err != nil {
		t.Fatal(err)
	}

	var mu sync.Mutex
	var ran []string
	fail := map[string]bool{"bad": true}
	exec := executor.Func(func(_ context.Context, r *scheduler.Reservation) error {
		mu.Lock()
		defer mu.Unlock()
		ran = append(ran, r.ID)
		if fail[r.ID] {
			return errors.New("启动失败")
		}
		return nil
	})

	runner := NewRunner(sch, fc, Minute, time.Minute, exec, WithEpoch(epoch))

	// a: [00:05, 00:10) 正常；bad: [00:05, 00:30) 执行失败；
	// c: [01:00, 02:00) 正常完成。
	for _, req := range []scheduler.Request{
		{ID: "a", Resource: "r", Interval: scheduler.Interval{Start: 5, End: 10}, Demand: scheduler.Demand{1}},
		{ID: "bad", Resource: "r", Interval: scheduler.Interval{Start: 5, End: 30}, Demand: scheduler.Demand{1}},
		{ID: "c", Resource: "r", Interval: scheduler.Interval{Start: 60, End: 120}, Demand: scheduler.Demand{1}},
	} {
		if _, err := sch.Reserve(req); err != nil {
			t.Fatal(err)
		}
	}

	// 推进到 00:05：a、bad 到点；bad 失败释放容量，a 运行中。
	fc.Set(epoch.Add(5 * time.Minute))
	runner.Step(context.Background())

	if got, _ := sch.Get("a"); got.Status != scheduler.StatusRunning {
		t.Fatalf("a 期望 running，得到 %s", got.Status)
	}
	if got, _ := sch.Get("bad"); got.Status != scheduler.StatusFailed {
		t.Fatalf("bad 期望 failed，得到 %s", got.Status)
	}
	mu.Lock()
	gotRan := append([]string(nil), ran...)
	mu.Unlock()
	if len(gotRan) != 2 {
		t.Fatalf("执行器应被调用 2 次，得到 %v", gotRan)
	}
	// bad 失败后其容量释放：[00:05,00:30) 可再预约。
	if _, err := sch.Reserve(scheduler.Request{
		Resource: "r", Interval: scheduler.Interval{Start: 5, End: 30}, Demand: scheduler.Demand{1},
	}); err != nil {
		t.Fatalf("失败预约应已释放容量: %v", err)
	}

	// 推进到 00:10：a 完成。
	fc.Set(epoch.Add(10 * time.Minute))
	runner.Step(context.Background())
	if got, _ := sch.Get("a"); got.Status != scheduler.StatusCompleted {
		t.Fatalf("a 期望 completed，得到 %s", got.Status)
	}

	// 推进到 03:00：c 早已结束。
	fc.Set(epoch.Add(180 * time.Minute))
	runner.Step(context.Background())
	if got, _ := sch.Get("c"); got.Status != scheduler.StatusCompleted {
		t.Fatalf("c 期望 completed，得到 %s", got.Status)
	}
}

func TestRunnerNoExecutorCallForZeroLengthStay(t *testing.T) {
	// 预约区间完全位于过去：一次 Step 内直接 start 并 complete，
	// 执行器不应被调用（见 dispatcher.step 的状态复核）。
	epoch := time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC)
	fc := clock.NewFake(epoch)
	sch := scheduler.New(nil)
	if _, err := sch.AddResource(scheduler.Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	if _, err := sch.Reserve(scheduler.Request{
		ID: "old", Resource: "r",
		Interval: scheduler.Interval{Start: 0, End: 10}, Demand: scheduler.Demand{1},
	}); err != nil {
		t.Fatal(err)
	}
	called := 0
	exec := executor.Func(func(context.Context, *scheduler.Reservation) error {
		called++
		return nil
	})
	runner := NewRunner(sch, fc, Minute, time.Minute, exec, WithEpoch(epoch))
	fc.Set(epoch.Add(20 * time.Minute))
	runner.Step(context.Background())
	if called != 0 {
		t.Fatalf("同一拍内已结束的预约不应调用执行器，实际调用 %d 次", called)
	}
	if got, _ := sch.Get("old"); got.Status != scheduler.StatusCompleted {
		t.Fatalf("old 期望 completed，得到 %s", got.Status)
	}
}

func TestRunnerRunStop(t *testing.T) {
	epoch := time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC)
	fc := clock.NewFake(epoch)
	sch := scheduler.New(nil)
	runner := NewRunner(sch, fc, Minute, 10*time.Millisecond, executor.Noop{}, WithEpoch(epoch))
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { runner.Run(ctx); close(done) }()
	time.Sleep(30 * time.Millisecond)
	cancel()
	runner.Stop()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("Stop 未能在超时内结束循环")
	}
}
