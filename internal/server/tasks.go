package server

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"runtime"
	"sync/atomic"
	"time"

	"worksteal/internal/executor"
)

// buildTaskFunc 把 HTTP 请求描述构造为可执行任务。
//
// 支持类型：
//   - noop：什么都不做（用于压测/健康检查）
//   - sleep：可取消地睡眠 ms 毫秒
//   - compute：做 iters 次无意义哈希，返回结果摘要
//   - tree：派生 fanout 个子任务、递归 depth 层；每个父任务等待所有子任务
//     （验收“深递归任务树 + help-the-child”的演示入口）
func buildTaskFunc(req submitReq) (executor.Func, []executor.Option, error) {
	opts := []executor.Option{}
	if req.Name != "" {
		opts = append(opts, executor.WithName(req.Name))
	}
	p := req.Params
	switch req.Type {
	case "", "noop":
		ms := paramInt(p, "ms", 0)
		return func(ctx context.Context, c executor.Context) error {
			if ms <= 0 {
				return nil
			}
			return sleepMS(ctx, time.Duration(ms)*time.Millisecond)
		}, opts, nil

	case "sleep":
		ms := paramInt(p, "ms", 100)
		return func(ctx context.Context, c executor.Context) error {
			return sleepMS(ctx, time.Duration(ms)*time.Millisecond)
		}, opts, nil

	case "compute":
		iters := paramInt(p, "iters", 1000)
		if iters < 0 {
			return nil, nil, errors.New("iters must be >= 0")
		}
		return func(ctx context.Context, c executor.Context) error {
			h := sha256.New()
			buf := []byte(c.TaskID())
			for i := 0; i < iters; i++ {
				if i&1023 == 0 {
					select {
					case <-ctx.Done():
						return ctx.Err()
					default:
					}
				}
				h.Write(buf)
				buf = h.Sum(nil)[:16]
			}
			sum := hex.EncodeToString(h.Sum(nil)[:8])
			_ = sum
			return nil
		}, opts, nil

	case "tree":
		depth := paramInt(p, "depth", 3)
		fanout := paramInt(p, "fanout", 2)
		workMS := paramInt(p, "work_ms", 0)
		if depth < 0 {
			return nil, nil, errors.New("depth must be >= 0")
		}
		if fanout < 1 {
			return nil, nil, errors.New("fanout must be >= 1")
		}
		if depth > 30 || fanout > 64 {
			return nil, nil, errors.New("refusing huge tree: depth<=30 and fanout<=64 over HTTP")
		}
		var totalNodes atomic.Int64
		fn := makeTreeFunc(depth, fanout, workMS, &totalNodes)
		return fn, opts, nil

	default:
		return nil, nil, fmt.Errorf("unknown task type %q", req.Type)
	}
}

// sleepMS 可被取消地睡眠。
func sleepMS(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return nil
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// makeTreeFunc 构造递归任务树：每个非叶节点派生 fanout 个孩子并等待全部完成。
// 这是“父等待子”结构在单 worker 下最容易触发饥饿死锁的形态，
// 也是 help-the-child 必须解决的场景。
func makeTreeFunc(depth, fanout, workMS int, counter *atomic.Int64) executor.Func {
	var build func(d int) executor.Func
	build = func(d int) executor.Func {
		return func(ctx context.Context, c executor.Context) error {
			counter.Add(1)
			if workMS > 0 {
				if err := sleepMS(ctx, time.Duration(workMS)*time.Millisecond); err != nil {
					return err
				}
			}
			if d == 0 {
				return nil
			}
			kids := make([]*executor.Task, fanout)
			var firstErr error
			for i := 0; i < fanout; i++ {
				t, err := c.Spawn(build(d - 1))
				if err != nil {
					return err
				}
				kids[i] = t
			}
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

func runtimeNumCPU() int {
	n := runtime.NumCPU()
	if n < 1 {
		return 1
	}
	return n
}
