package executor_test

import (
	"context"
	"errors"
	"fmt"
	"math/rand"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"worksteal/internal/event"
	"worksteal/internal/executor"
)

// collector 订阅事件总线。事件先堆积在大容量订阅通道里；snapshot 时
// 在调用方同步排空并累加，避免独立消费 goroutine 在执行器 Wait 返回后
// 尚未读完最后几条事件的时序假象。
type collector struct {
	mu     sync.Mutex
	sub    *event.Subscription
	events []event.Event
}

func newCollector(bus *event.Bus) *collector {
	sub, history := bus.Subscribe(1 << 20)
	c := &collector{sub: sub}
	c.events = append(c.events, history...)
	return c
}

func (c *collector) snapshot() []event.Event {
	c.mu.Lock()
	defer c.mu.Unlock()
	for {
		select {
		case ev, ok := <-c.sub.C():
			if !ok {
				goto done
			}
			c.events = append(c.events, ev)
		default:
			goto done
		}
	}
done:
	out := make([]event.Event, len(c.events))
	copy(out, c.events)
	return out
}

// verifyAtMostOnceAndTerminal 是验收核心不变量：
//  1. 每个任务的 task_started 至多一条（任务体至多执行一次）；
//  2. 每个任务恰好一条终结事件（completed/failed/canceled/panicked）；
//  3. 每条 started 都能对应到一条终结；未 started 的必须是 canceled。
func verifyAtMostOnceAndTerminal(t *testing.T, evs []event.Event) (started, terminal, total int) {
	t.Helper()
	starts := map[string]int{}
	terms := map[string]event.Kind{}
	known := map[string]struct{}{}
	for _, ev := range evs {
		if ev.TaskID == "" {
			continue
		}
		switch ev.Kind {
		case event.KindTaskSubmitted, event.KindTaskSpawned:
			known[ev.TaskID] = struct{}{}
		case event.KindTaskStarted:
			known[ev.TaskID] = struct{}{}
			starts[ev.TaskID]++
		case event.KindTaskCompleted, event.KindTaskFailed, event.KindTaskCanceled, event.KindTaskPanicked:
			known[ev.TaskID] = struct{}{}
			if prev, ok := terms[ev.TaskID]; ok {
				t.Errorf("task %s terminalized twice: %s then %s", ev.TaskID, prev, ev.Kind)
			}
			terms[ev.TaskID] = ev.Kind
		}
	}
	for id := range known {
		n := starts[id]
		if n > 1 {
			t.Errorf("task %s started %d times, want <= 1", id, n)
		}
		term, ok := terms[id]
		if !ok {
			t.Errorf("task %s has no terminal event", id)
			continue
		}
		if n == 0 && term != event.KindTaskCanceled {
			t.Errorf("task %s never started but terminated as %s", id, term)
		}
		started += n
		terminal++
	}
	return started, terminal, len(known)
}

func TestBasicSubmitComplete(t *testing.T) {
	for _, kind := range []string{"chaselev", "mutex"} {
		t.Run(kind, func(t *testing.T) {
			ex := executor.New(executor.Config{Workers: 2, DequeKind: kind})
			var n atomic.Int64
			tasks := make([]*executor.Task, 100)
			var err error
			for i := range tasks {
				tasks[i], err = ex.Submit(func(ctx context.Context, c executor.Context) error {
					n.Add(1)
					return nil
				})
				if err != nil {
					t.Fatal(err)
				}
			}
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			if err := ex.Wait(ctx); err != nil {
				t.Fatalf("wait: %v", err)
			}
			if n.Load() != 100 {
				t.Fatalf("ran %d, want 100", n.Load())
			}
			for _, task := range tasks {
				if task.Runs() != 1 || task.Status() != executor.StatusCompleted {
					t.Fatalf("task %s runs=%d status=%s", task.ID(), task.Runs(), task.Status())
				}
			}
			if err := ex.Shutdown(ctx); err != nil {
				t.Fatal(err)
			}
		})
	}
}

// TestDeepTreeSingleWorker 验收：单 worker 下深递归任务树必须完成——
// 朴素的“阻塞等待子任务”实现会死锁；help-the-child 让等待中的 worker
// 亲自执行子任务。
func TestDeepTreeSingleWorker(t *testing.T) {
	for _, kind := range []string{"chaselev", "mutex"} {
		t.Run(kind, func(t *testing.T) {
			ex := executor.New(executor.Config{Workers: 1, DequeKind: kind, Name: "single-" + kind})
			coll := newCollector(ex.Bus())
			const depth = 12
			var nodes atomic.Int64
			root, err := ex.Submit(treeFunc(depth, 2, 0, &nodes))
			if err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			if err := ex.Wait(ctx); err != nil {
				t.Fatalf("deep tree deadlocked? wait: %v (nodes so far=%d)", err, nodes.Load())
			}
			want := (1 << (depth + 1)) - 1 // 满二叉树节点数
			if nodes.Load() != int64(want) {
				t.Fatalf("executed nodes=%d want %d", nodes.Load(), want)
			}
			if root.Status() != executor.StatusCompleted || root.Err() != nil {
				t.Fatalf("root status=%s err=%v", root.Status(), root.Err())
			}
			evs := coll.snapshot()
			started, terminal, total := verifyAtMostOnceAndTerminal(t, evs)
			if started != want || terminal != want || total != want {
				t.Fatalf("started=%d terminal=%d total=%d want=%d", started, terminal, total, want)
			}
			if err := ex.Shutdown(ctx); err != nil {
				t.Fatal(err)
			}
		})
	}
}

// TestDeepTreeMultiWorker 多 worker 下树也必须全部完成，且发生过窃取。
func TestDeepTreeMultiWorker(t *testing.T) {
	ex := executor.New(executor.Config{Workers: 4, Name: "multi"})
	var nodes atomic.Int64
	const depth = 10
	if _, err := ex.Submit(treeFunc(depth, 3, 1, &nodes)); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := ex.Wait(ctx); err != nil {
		t.Fatalf("wait: %v", err)
	}
	want := int(1)
	childCount := 1
	for d := 0; d < depth; d++ {
		childCount *= 3
		want += childCount
	}
	if nodes.Load() != int64(want) {
		t.Fatalf("nodes=%d want %d", nodes.Load(), want)
	}
	st := ex.Stats()
	if st.Steals == 0 {
		t.Fatalf("expected steals with 4 workers, stats=%+v", st)
	}
	if err := ex.Shutdown(ctx); err != nil {
		t.Fatal(err)
	}
}

// TestRandomCancellation 随机取消验收：在树执行过程中从外部随机取消
// 任意已出现的任务（含根），跨多种子与 worker 数（含 1）。
// 检查：每个任务至多执行一次、每个任务有终结、执行器最终可退出。
func TestRandomCancellation(t *testing.T) {
	seeds := []int64{1, 42, 7, 2026}
	workerCounts := []int{1, 4}
	for _, workers := range workerCounts {
		for _, seed := range seeds {
			t.Run(fmt.Sprintf("workers=%d/seed=%d", workers, seed), func(t *testing.T) {
				runRandomCancel(t, workers, seed)
			})
		}
	}
}

func runRandomCancel(t *testing.T, workers int, seed int64) {
	t.Helper()
	name := fmt.Sprintf("cancel-w%d-s%d", workers, seed)
	ex := executor.New(executor.Config{Workers: workers, Name: name})
	coll := newCollector(ex.Bus())

	const depth = 6
	const fanout = 3
	var nodes atomic.Int64
	root, err := ex.Submit(treeFunc(depth, fanout, 1, &nodes))
	if err != nil {
		t.Fatal(err)
	}

	rng := rand.New(rand.NewSource(seed))
	cancelDone := make(chan struct{})

	// 从事件流收集已出现的任务 ID，随机取消其中的一些，直到根终结。
	go func() {
		defer close(cancelDone)
		seen := map[string]struct{}{}
		tick := time.NewTicker(time.Microsecond * 200)
		defer tick.Stop()
		for {
			select {
			case <-root.Done():
				return
			case <-tick.C:
				for _, ev := range coll.snapshot() {
					if ev.Kind == event.KindTaskSpawned || ev.Kind == event.KindTaskSubmitted {
						seen[ev.TaskID] = struct{}{}
					}
				}
				// 每轮取消 1~3 个随机任务。
				k := 1 + rng.Intn(3)
				ids := make([]string, 0, len(seen))
				for id := range seen {
					ids = append(ids, id)
				}
				rng.Shuffle(len(ids), func(i, j int) { ids[i], ids[j] = ids[j], ids[i] })
				if len(ids) > k {
					ids = ids[:k]
				}
				for _, id := range ids {
					ex.Cancel(id, errors.New("random test cancel"))
				}
			}
		}
	}()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	// 根可能被取消而失败，因此不等待根成功，而是等执行器完全排空。
	if err := ex.Wait(ctx); err != nil {
		t.Fatalf("executor did not quiesce: %v (nodes=%d)", err, nodes.Load())
	}
	<-cancelDone

	// 验收不变量。
	evs := coll.snapshot()
	started, terminal, total := verifyAtMostOnceAndTerminal(t, evs)
	if started > total || terminal != total {
		t.Fatalf("started=%d terminal=%d total=%d", started, terminal, total)
	}
	// 计数器一致性：started == completed+failed+canceled 中真正跑过的部分；
	// submitted+spawned == total。
	st := ex.Stats()
	if st.Submitted+st.Spawned != int64(total) {
		t.Fatalf("submitted=%d spawned=%d total=%d", st.Submitted, st.Spawned, total)
	}
	if st.Started != int64(started) {
		t.Fatalf("stats started=%d but events=%d", st.Started, started)
	}
	if st.Completed+st.Failed+st.Canceled != int64(total) {
		t.Fatalf("completed=%d failed=%d canceled=%d total=%d",
			st.Completed, st.Failed, st.Canceled, total)
	}

	// 最终可退出：优雅关闭必须按时完成（若有计数泄漏这里会超时）。
	shCtx, shCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shCancel()
	if err := ex.Shutdown(shCtx); err != nil {
		t.Fatalf("executor failed to exit: %v", err)
	}
}

// TestCancelQueuedNeverRuns 排队中取消的任务绝不执行。
func TestCancelQueuedNeverRuns(t *testing.T) {
	ex := executor.New(executor.Config{Workers: 1})
	block := make(chan struct{})
	gate := make(chan struct{})
	// 先占住唯一 worker。
	holder, err := ex.Submit(func(ctx context.Context, c executor.Context) error {
		close(gate)
		select {
		case <-block:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	<-gate
	var ran atomic.Bool
	victim, err := ex.Submit(func(ctx context.Context, c executor.Context) error {
		ran.Store(true)
		return nil
	}, executor.WithName("victim"))
	if err != nil {
		t.Fatal(err)
	}
	// 给点时间确保 victim 已入队但无法被执行。
	time.Sleep(20 * time.Millisecond)
	if !ex.Cancel(victim.ID(), errors.New("nope")) {
		t.Fatal("cancel should be accepted")
	}
	close(block)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := ex.Wait(ctx); err != nil {
		t.Fatal(err)
	}
	if ran.Load() {
		t.Fatal("canceled queued task executed")
	}
	if victim.Status() != executor.StatusCanceled || victim.Runs() != 0 {
		t.Fatalf("status=%s runs=%d", victim.Status(), victim.Runs())
	}
	if holder.Status() != executor.StatusCompleted {
		t.Fatalf("holder status=%s", holder.Status())
	}
	_ = ex.Shutdown(ctx)
}

// TestCancelRunningPropagates 取消运行中任务：ctx 取消并级联到派生任务。
func TestCancelRunningPropagates(t *testing.T) {
	ex := executor.New(executor.Config{Workers: 2})
	coll := newCollector(ex.Bus())
	rootStarted := make(chan struct{})
	root, err := ex.Submit(func(ctx context.Context, c executor.Context) error {
		child, err := c.Spawn(func(ctx context.Context, c executor.Context) error {
			<-ctx.Done()
			return ctx.Err()
		})
		if err != nil {
			return err
		}
		close(rootStarted)
		if err := c.Wait(child); err != nil {
			return err
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	<-rootStarted
	time.Sleep(10 * time.Millisecond)
	if !ex.Cancel(root.ID(), errors.New("abort")) {
		t.Fatal("cancel root")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := ex.Wait(ctx); err != nil {
		t.Fatal(err)
	}
	if root.Status() != executor.StatusCanceled {
		t.Fatalf("root status=%s err=%v", root.Status(), root.Err())
	}
	_, terminal, _ := verifyAtMostOnceAndTerminal(t, coll.snapshot())
	if terminal != 2 { // root + child
		t.Fatalf("terminal tasks=%d want 2", terminal)
	}
	_ = ex.Shutdown(ctx)
}

// TestPanicIsolated panic 任务记为 failed/panicked，worker 继续服务。
func TestPanicIsolated(t *testing.T) {
	ex := executor.New(executor.Config{Workers: 1})
	bad, err := ex.Submit(func(ctx context.Context, c executor.Context) error {
		panic("boom")
	})
	if err != nil {
		t.Fatal(err)
	}
	var ok atomic.Bool
	good, err := ex.Submit(func(ctx context.Context, c executor.Context) error {
		ok.Store(true)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := ex.Wait(ctx); err != nil {
		t.Fatal(err)
	}
	var pe *executor.PanicError
	if !errors.As(bad.Err(), &pe) {
		t.Fatalf("bad err=%v want PanicError", bad.Err())
	}
	if bad.Runs() != 1 || !ok.Load() || good.Status() != executor.StatusCompleted {
		t.Fatalf("bad runs=%d ok=%v good=%s", bad.Runs(), ok.Load(), good.Status())
	}
	_ = ex.Shutdown(ctx)
}

// TestShutdownNowExit 立即关闭：未启动任务取消，执行器快速退出。
func TestShutdownNowExit(t *testing.T) {
	// 单 worker：阻塞任务占住唯一 worker，第二个任务必然排队，
	// ShutdownNow 后它必须被取消而不是执行。
	ex := executor.New(executor.Config{Workers: 1})
	block := make(chan struct{})
	_, _ = ex.Submit(func(ctx context.Context, c executor.Context) error {
		select {
		case <-block:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	})
	var queuedRan atomic.Bool
	_, _ = ex.Submit(func(ctx context.Context, c executor.Context) error {
		queuedRan.Store(true)
		return nil
	})
	time.Sleep(20 * time.Millisecond)
	done := make(chan struct{})
	var pending []executor.Snapshot
	go func() {
		pending = ex.ShutdownNow()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("ShutdownNow did not return")
	}
	_ = pending
	if queuedRan.Load() {
		t.Fatal("queued task ran after ShutdownNow")
	}
}

// treeFunc 构造递归任务树：每个节点派生 fanout 个孩子并等待全部。
// 所有睡眠都响应 ctx 取消。
func treeFunc(depth, fanout, workMS int, counter *atomic.Int64) executor.Func {
	var build func(d int) executor.Func
	build = func(d int) executor.Func {
		return func(ctx context.Context, c executor.Context) error {
			counter.Add(1)
			if workMS > 0 {
				tm := time.NewTimer(time.Duration(workMS) * time.Millisecond)
				select {
				case <-tm.C:
				case <-ctx.Done():
					tm.Stop()
					return ctx.Err()
				}
			}
			if d == 0 {
				return nil
			}
			kids := make([]*executor.Task, fanout)
			for i := 0; i < fanout; i++ {
				t, err := c.Spawn(build(d - 1))
				if err != nil {
					return err
				}
				kids[i] = t
			}
			var firstErr error
			for _, t := range kids {
				if err := c.Wait(t); err != nil && firstErr == nil {
					firstErr = err
				}
			}
			return firstErr
		}
	}
	return build(depth)
}
