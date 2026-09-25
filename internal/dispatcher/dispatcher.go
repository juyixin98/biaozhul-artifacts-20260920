// Package dispatcher 把可替换时钟、执行器与调度库编排起来：
// 按固定节奏观察时钟，推进调度状态，并在预约开始时调用执行器。
package dispatcher

import (
	"context"
	"log"
	"time"

	"resourcebooking/internal/clock"
	"resourcebooking/internal/executor"
	"resourcebooking/internal/scheduler"
)

// TickDuration 定义一个调度 tick 对应的墙钟时长。
// 调度库使用整数 tick（例如“分钟数”），dispatcher 负责
// 墙钟时间与 tick 之间的换算。
type TickDuration time.Duration

// Minute 是最常用的 tick 粒度。
const Minute TickDuration = TickDuration(time.Minute)

// Runner 周期性地推进调度器。
type Runner struct {
	sch  *scheduler.Scheduler
	clk  clock.Clock
	exec executor.Executor
	td   TickDuration
	// epoch 是 tick=0 对应的墙钟时刻（UTC）。
	epoch time.Time
	every time.Duration
	logf  func(format string, args ...any)

	cancel context.CancelFunc
	done   chan struct{}
}

// Option 配置 Runner。
type Option func(*Runner)

// WithEpoch 设置 tick=0 对应的墙钟基准时刻。
func WithEpoch(t time.Time) Option {
	return func(r *Runner) { r.epoch = t.UTC() }
}

// WithLogger 替换默认的标准日志输出；传 nil 可静默。
func WithLogger(f func(format string, args ...any)) Option {
	return func(r *Runner) { r.logf = f }
}

// NewRunner 创建分发器。
//
// tick 为一个整数刻度的墙钟长度（通常是 Minute）；every 为
// 轮询间隔（通常等于 tick）。exec 为 nil 时使用 Noop 执行器。
func NewRunner(sch *scheduler.Scheduler, clk clock.Clock, tick TickDuration, every time.Duration, exec executor.Executor, opts ...Option) *Runner {
	if tick <= 0 {
		tick = Minute
	}
	if every <= 0 {
		every = time.Duration(tick)
	}
	if exec == nil {
		exec = executor.Noop{}
	}
	if clk == nil {
		clk = clock.Wall{}
	}
	r := &Runner{
		sch:   sch,
		clk:   clk,
		exec:  exec,
		td:    tick,
		epoch: time.Unix(0, 0).UTC(),
		every: every,
		logf:  log.Printf,
	}
	for _, o := range opts {
		o(r)
	}
	return r
}

// TicksAt 把墙钟时刻换算为整数 tick（向负无穷截断）。
func (r *Runner) TicksAt(t time.Time) scheduler.Ticks {
	d := t.UTC().Sub(r.epoch)
	return scheduler.Ticks(d / time.Duration(r.td))
}

// Run 启动后台循环，直到 ctx 取消。返回前循环已完全停止。
func (r *Runner) Run(ctx context.Context) {
	ctx, r.cancel = context.WithCancel(ctx)
	r.done = make(chan struct{})
	defer close(r.done)

	t := time.NewTicker(r.every)
	defer t.Stop()
	// 启动时立即推进一次，不必等待第一个 tick。
	r.step(ctx)
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			r.step(ctx)
		}
	}
}

// Stop 停止后台循环并等待其退出。
func (r *Runner) Stop() {
	if r.cancel != nil {
		r.cancel()
	}
	if r.done != nil {
		<-r.done
	}
}

// Step 暴露单次推进，便于测试与手动驱动。
func (r *Runner) Step(ctx context.Context) { r.step(ctx) }

func (r *Runner) step(ctx context.Context) {
	nowTick := r.TicksAt(r.clk.Now())
	res := r.sch.Advance(nowTick)
	for _, id := range res.Started {
		rec, err := r.sch.Get(id)
		if err != nil {
			continue
		}
		// 同一拍内开始且已结束（End <= now）的预约已被 Advance
		// 直接标记为 completed，不再调用执行器。
		if rec.Status != scheduler.StatusRunning {
			continue
		}
		if err := r.exec.Run(ctx, rec); err != nil {
			if _, ferr := r.sch.MarkFailed(id, err.Error()); ferr != nil && r.logf != nil {
				r.logf("dispatcher: 标记预约 %s 失败时出错: %v", id, ferr)
			}
			if r.logf != nil {
				r.logf("dispatcher: 预约 %s 执行器失败: %v", id, err)
			}
		}
	}
}
