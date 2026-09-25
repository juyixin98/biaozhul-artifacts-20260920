package executor

import (
	"context"
	"errors"
	"testing"

	"resourcebooking/internal/scheduler"
)

func TestNoop(t *testing.T) {
	noop := Noop{}
	if err := noop.Run(context.Background(), &scheduler.Reservation{ID: "x"}); err != nil {
		t.Fatalf("Noop 不应失败: %v", err)
	}
}

func TestFunc(t *testing.T) {
	wantErr := errors.New("boom")
	called := false
	var f Executor = Func(func(_ context.Context, r *scheduler.Reservation) error {
		called = true
		if r.ID != "x" {
			t.Fatalf("收到错误预约: %s", r.ID)
		}
		return wantErr
	})
	err := f.Run(context.Background(), &scheduler.Reservation{ID: "x"})
	if !called || !errors.Is(err, wantErr) {
		t.Fatalf("Func 行为异常: called=%v err=%v", called, err)
	}
}
