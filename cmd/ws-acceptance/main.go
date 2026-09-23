// ws-acceptance 是命令行验收程序，显式演示并核对四条验收标准：
//
//  1. 深递归任务树在单 worker 模式下可以完成（help-the-child 防饥饿死锁）；
//  2. 执行过程中随机取消任意任务（含根），系统保持正确；
//  3. 每个任务至多执行一次，且每个任务都进入唯一终态；
//  4. 最终执行器可以干净退出（无计数泄漏、无 goroutine 卡死）。
//
// 它不依赖 HTTP，直接使用调度库；退出码 0 表示全部通过。
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"math/rand"
	"os"
	"sync/atomic"
	"time"

	"worksteal/internal/event"
	"worksteal/internal/executor"
)

func main() {
	depth := flag.Int("depth", 10, "递归树深度")
	fanout := flag.Int("fanout", 2, "每个节点的子任务数")
	workers := flag.Int("workers", 1, "worker 数（验收用 1 模拟最严苛单线程）")
	seed := flag.Int64("seed", 20260924, "随机取消种子")
	cancelEvery := flag.Duration("cancel-every", 10*time.Millisecond, "随机取消尝试间隔")
	workMS := flag.Int("work-ms", 1, "每个节点的可取消工作量（毫秒），用于扩大排队窗口让取消真正发生")
	flag.Parse()

	failures := 0
	check := func(name string, ok bool, detail string) {
		status := "PASS"
		if !ok {
			status = "FAIL"
			failures++
		}
		fmt.Printf("[%s] %s  %s\n", status, name, detail)
	}

	// ---------- 场景 A：无取消的深递归单 worker 树 ----------
	fmt.Println("== 场景 A：单 worker 深递归任务树（无取消） ==")
	resA := runTree(*workers, *depth, *fanout, 0, false, 300*time.Microsecond, 0)
	wantNodes := geometricSum(*fanout, *depth)
	check("A1 所有节点恰好执行一次",
		resA.started == wantNodes && resA.duplicateStarts == 0,
		fmt.Sprintf("started=%d want=%d duplicateStarts=%d", resA.started, wantNodes, resA.duplicateStarts))
	check("A2 每个任务都有唯一终态",
		resA.terminal == wantNodes && resA.unterminated == 0 && resA.doubleTerminal == 0,
		fmt.Sprintf("terminal=%d unterminated=%d doubleTerminal=%d", resA.terminal, resA.unterminated, resA.doubleTerminal))
	check("A3 单 worker 下未发生饥饿死锁（按时排空）",
		resA.quiesced, fmt.Sprintf("quiesced=%v elapsed=%s", resA.quiesced, resA.elapsed))
	check("A4 执行器可干净退出", resA.exited, fmt.Sprintf("exited=%v", resA.exited))

	// ---------- 场景 B：随机取消 ----------
	fmt.Println("== 场景 B：单 worker 深树 + 随机取消 ==")
	resB := runTree(1, *depth-2, *fanout, *seed, true, *cancelEvery, *workMS)
	check("B1 取消后每个任务仍至多执行一次",
		resB.duplicateStarts == 0,
		fmt.Sprintf("started=%d duplicateStarts=%d", resB.started, resB.duplicateStarts))
	check("B2 每个已产生任务都进入唯一终态",
		resB.unterminated == 0 && resB.doubleTerminal == 0,
		fmt.Sprintf("produced=%d terminal=%d unterminated=%d doubleTerminal=%d",
			resB.produced, resB.terminal, resB.unterminated, resB.doubleTerminal))
	check("B3 终态分类计数守恒",
		resB.completed+resB.failed+resB.canceled == int64(resB.produced),
		fmt.Sprintf("completed=%d failed=%d canceled=%d produced=%d",
			resB.completed, resB.failed, resB.canceled, resB.produced))
	check("B4 随机取消确实生效（canceled>0）",
		resB.canceled > 0,
		fmt.Sprintf("canceled=%d", resB.canceled))
	check("B5 随机取消下执行器按时排空",
		resB.quiesced, fmt.Sprintf("quiesced=%v elapsed=%s", resB.quiesced, resB.elapsed))
	check("B6 最终可退出", resB.exited, fmt.Sprintf("exited=%v", resB.exited))

	// ---------- 场景 C：多 worker + 随机取消（窃取路径） ----------
	fmt.Println("== 场景 C：4 worker 深树 + 随机取消（覆盖窃取） ==")
	resC := runTree(4, *depth-2, *fanout+1, *seed+1, true, *cancelEvery, *workMS)
	check("C1 至多执行一次", resC.duplicateStarts == 0,
		fmt.Sprintf("duplicateStarts=%d steals=%d", resC.duplicateStarts, resC.steals))
	check("C2 唯一终态且可退出",
		resC.unterminated == 0 && resC.doubleTerminal == 0 && resC.exited,
		fmt.Sprintf("unterminated=%d doubleTerminal=%d exited=%v steals=%d",
			resC.unterminated, resC.doubleTerminal, resC.exited, resC.steals))
	check("C3 多 worker 下取消确实生效", resC.canceled > 0,
		fmt.Sprintf("canceled=%d", resC.canceled))

	fmt.Println()
	if failures == 0 {
		fmt.Printf("全部验收通过 ✅（种子 %d）\n", *seed)
		os.Exit(0)
	}
	fmt.Printf("有 %d 项未通过 ❌\n", failures)
	os.Exit(1)
}

type result struct {
	produced, started, terminal  int
	duplicateStarts              int
	unterminated, doubleTerminal int
	completed, failed, canceled  int64
	steals                       int64
	quiesced, exited             bool
	elapsed                      time.Duration
}

func runTree(workers, depth, fanout int, seed int64, cancel bool, args ...any) result {
	interval := 300 * time.Microsecond
	workMS := 1
	if len(args) > 0 {
		if v, ok := args[0].(time.Duration); ok {
			interval = v
		}
	}
	if len(args) > 1 {
		if v, ok := args[1].(int); ok {
			workMS = v
		}
	}
	name := fmt.Sprintf("acc-w%d-s%d", workers, seed)
	ex := executor.New(executor.Config{Workers: workers, Name: name})
	bus := ex.Bus()
	sub, history := bus.Subscribe(1 << 20)
	allEvents := history

	var nodes atomic.Int64
	root, _ := ex.Submit(tree(depth, fanout, workMS, &nodes))

	cancelDone := make(chan struct{})
	if cancel {
		go func() {
			defer close(cancelDone)
			rng := rand.New(rand.NewSource(seed))
			ticker := time.NewTicker(interval)
			defer ticker.Stop()
			for {
				select {
				case <-root.Done():
					return
				case <-ticker.C:
					// 同步排空当前事件，随机挑 1~3 个已出现任务取消。
					allEvents = append(allEvents, sub.Drain()...)
					seen := map[string]struct{}{}
					for _, ev := range allEvents {
						if ev.Kind == event.KindTaskSpawned || ev.Kind == event.KindTaskSubmitted {
							seen[ev.TaskID] = struct{}{}
						}
					}
					ids := make([]string, 0, len(seen))
					for id := range seen {
						if id == root.ID() {
							continue // 不取消根：保证得到“部分完成+部分取消”的混合树
						}
						ids = append(ids, id)
					}
					rng.Shuffle(len(ids), func(i, j int) { ids[i], ids[j] = ids[j], ids[i] })
					k := 1 + rng.Intn(3)
					if k > len(ids) {
						k = len(ids)
					}
					for _, id := range ids[:k] {
						ex.Cancel(id, errors.New("acceptance random cancel"))
					}
				}
			}
		}()
	}

	start := time.Now()
	ctx, cfn := context.WithTimeout(context.Background(), 60*time.Second)
	waitErr := ex.Wait(ctx)
	elapsed := time.Since(start)
	cfn()
	if cancel {
		<-cancelDone
	}
	allEvents = append(allEvents, sub.Drain()...)
	sub.Close()

	st := ex.Stats()
	r := analyze(allEvents)
	r.steals = st.Steals
	r.quiesced = waitErr == nil
	r.elapsed = elapsed
	r.completed, r.failed, r.canceled = st.Completed, st.Failed, st.Canceled

	shCtx, shCancel := context.WithTimeout(context.Background(), 10*time.Second)
	shErr := ex.Shutdown(shCtx)
	shCancel()
	r.exited = shErr == nil
	return r
}

func analyze(evs []event.Event) result {
	var r result
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
		case event.KindTaskCompleted, event.KindTaskFailed, event.KindTaskCanceled:
			known[ev.TaskID] = struct{}{}
			if _, ok := terms[ev.TaskID]; ok {
				r.doubleTerminal++
			}
			terms[ev.TaskID] = ev.Kind
		}
	}
	r.produced = len(known)
	for id := range known {
		n := starts[id]
		r.started += n
		if n > 1 {
			r.duplicateStarts += n - 1
		}
		if _, ok := terms[id]; !ok {
			r.unterminated++
		} else {
			r.terminal++
		}
	}
	return r
}

func geometricSum(fanout, depth int) int {
	n, power := 1, 1
	for d := 0; d < depth; d++ {
		power *= fanout
		n += power
	}
	return n
}

// tree 构造每个节点派生 fanout 个孩子并等待全部的递归任务。
// workMS>0 时每个节点先做一段可取消的睡眠，扩大排队窗口。
func tree(depth, fanout, workMS int, counter *atomic.Int64) executor.Func {
	var build func(int) executor.Func
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
