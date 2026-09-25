// Package executor 定义预约开始时被调用的执行器抽象。
//
// 调度库只负责“在什么时候、占多少容量”；到点之后要做什么
// （下单、开机、发通知……）由 Executor 的实现决定。测试中可
// 替换为 Func / 可控失败的脚本执行器。
package executor

import (
	"context"

	"resourcebooking/internal/scheduler"
)

// Executor 在预约开始时被调用一次。
//
// 返回 error 表示启动失败，dispatcher 会把预约标记为 failed
// 并立即释放其容量；返回 nil 表示运行已开始（本接口不追踪
// 运行过程，到点后由 dispatcher 正常标记 completed）。
type Executor interface {
	Run(ctx context.Context, r *scheduler.Reservation) error
}

// Noop 什么都不做，永远成功。
type Noop struct{}

// Run 实现 Executor。
func (Noop) Run(context.Context, *scheduler.Reservation) error { return nil }

// Func 用一个函数快速构造执行器。
type Func func(ctx context.Context, r *scheduler.Reservation) error

// Run 实现 Executor。
func (f Func) Run(ctx context.Context, r *scheduler.Reservation) error { return f(ctx, r) }
