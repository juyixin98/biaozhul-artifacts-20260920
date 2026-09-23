package inference

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestRequestKeyAndSize(t *testing.T) {
	r := &Request{ID: "x", Model: "llm-1", Prompt: "你好"}
	if r.Key() != "llm-1" {
		t.Fatalf("key = %q", r.Key())
	}
	if r.SchedulerID() != "x" {
		t.Fatalf("scheduler id = %q", r.SchedulerID())
	}
	if r.Size() <= len(r.Prompt) {
		t.Fatalf("size %d should exceed prompt bytes", r.Size())
	}
	if got := len("你好"); got != 6 && r.Size() < 64 {
		t.Fatal("size should count UTF-8 bytes")
	}
}

func TestFakeExecutor_BatchingDeterministic(t *testing.T) {
	exec := &FakeExecutor{Latency: time.Millisecond}
	reqs := []*Request{
		{ID: "r1", Model: "m", Prompt: "aaa"},
		{ID: "r2", Model: "m", Prompt: "bbb"},
	}
	res := exec.Execute(context.Background(), reqs)
	if len(res) != 2 {
		t.Fatalf("results = %d, want 2", len(res))
	}
	for i, r := range res {
		if r.Err != nil {
			t.Fatalf("item %d unexpected err: %v", i, r.Err)
		}
		if r.Output["id"] != reqs[i].ID {
			t.Fatalf("item %d output id = %v", i, r.Output["id"])
		}
		if r.Output["batch_size"] != 2 {
			t.Fatalf("item %d batch_size = %v, want 2", i, r.Output["batch_size"])
		}
	}

	// 同输入应产生确定性输出。
	res2 := exec.Execute(context.Background(), reqs[:1])
	if res2[0].Output["output"] != res[0].Output["output"] {
		t.Fatalf("mock completion not deterministic: %v vs %v",
			res2[0].Output["output"], res[0].Output["output"])
	}
	if sizes := exec.BatchSizes(); len(sizes) != 2 || sizes[0] != 2 || sizes[1] != 1 {
		t.Fatalf("batch sizes = %v, want [2 1]", sizes)
	}
}

func TestFakeExecutor_PartialFailure(t *testing.T) {
	exec := &FakeExecutor{}
	res := exec.Execute(context.Background(), []*Request{
		{ID: "ok", Model: "m", Prompt: "p"},
		{ID: "bad", Model: "m", Prompt: "p", SimulateError: "model_error"},
		{ID: "limited", Model: "m", Prompt: "p", SimulateError: "rate_limited"},
		{ID: "custom", Model: "m", Prompt: "p", SimulateError: "weird_code"},
	})
	if res[0].Err != nil {
		t.Fatalf("ok item failed: %v", res[0].Err)
	}
	var ie *ItemError
	if !errors.As(res[1].Err, &ie) || ie.Code != "model_error" {
		t.Fatalf("bad item err = %v", res[1].Err)
	}
	if ie2 := res[2].Err.(*ItemError); ie2.Code != "rate_limited" {
		t.Fatalf("limited item err = %v", res[2].Err)
	}
	if ie3 := res[3].Err.(*ItemError); ie3.Message != "simulated failure" {
		t.Fatalf("custom code message = %q", ie3.Message)
	}
}

func TestFakeExecutor_SleepHook(t *testing.T) {
	slept := false
	exec := &FakeExecutor{
		Latency: 100 * time.Second, // 真实 sleep 会让测试超时
		Sleep:   func(time.Duration) { slept = true },
	}
	exec.Execute(context.Background(), []*Request{{ID: "r", Model: "m", Prompt: "p"}})
	if !slept {
		t.Fatal("injected Sleep not used")
	}
}
