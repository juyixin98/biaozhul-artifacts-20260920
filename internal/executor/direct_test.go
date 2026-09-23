package executor_test

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"worksteal/internal/executor"
)

// TestDirectExecutesInlineTree 可替换执行器：Direct 内联执行整棵
// Spawn/Wait 树，单调用栈上靠 help-the-child 完成，不死锁。
func TestDirectExecutesInlineTree(t *testing.T) {
	d := executor.NewDirect("direct-1", nil, nil)
	var nodes atomic.Int64
	root, err := d.Submit(treeFunc(6, 2, 0, &nodes))
	if err != nil {
		t.Fatal(err)
	}
	if nodes.Load() != 127 {
		t.Fatalf("nodes=%d want 127", nodes.Load())
	}
	if root.Status() != executor.StatusCompleted {
		t.Fatalf("root=%s err=%v", root.Status(), root.Err())
	}
	if err := d.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
}

// TestSwapExecutorImplementation 同一任务代码跑在两种执行器上结果一致，
// 证明调度/任务逻辑不绑定具体执行器实现（可替换）。
func TestSwapExecutorImplementation(t *testing.T) {
	run := func(name string, submit func(executor.Func, ...executor.Option) (*executor.Task, error), wait func(context.Context) error, shutdown func(context.Context) error) {
		var nodes atomic.Int64
		root, err := submit(treeFunc(7, 2, 0, &nodes))
		if err != nil {
			t.Fatalf("%s submit: %v", name, err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := wait(ctx); err != nil {
			t.Fatalf("%s wait: %v", name, err)
		}
		if nodes.Load() != 255 || root.Status() != executor.StatusCompleted {
			t.Fatalf("%s nodes=%d root=%s", name, nodes.Load(), root.Status())
		}
		if err := shutdown(ctx); err != nil {
			t.Fatalf("%s shutdown: %v", name, err)
		}
	}

	pooled := executor.New(executor.Config{Workers: 3, Name: "pool"})
	run("pool", pooled.Submit, pooled.Wait, pooled.Shutdown)

	direct := executor.NewDirect("direct", nil, nil)
	run("direct", direct.Submit, direct.Wait, direct.Shutdown)
}
