package faultclient

import (
	"net/http"
	"testing"
	"time"

	"contractcheck/internal/clock"
)

// 使用假时钟+假睡：重试退避不占用真实时间，测试快速且确定。
func newFakeDeps(start time.Time) (*clock.Fake, *FakeSleeper) {
	clk := clock.NewFake(start)
	return clk, NewFakeSleeper(clk)
}

func TestNoFault_SucceedsFirstTry(t *testing.T) {
	clk, sl := newFakeDeps(time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC))
	srv := NewFakeServer(FaultSpec{Kind: FaultNone})
	defer srv.Close()

	c := New(Config{MaxAttempts: 3, InitialBackoff: time.Millisecond}, clk, sl)
	res := c.Get(srv.URL)
	if !res.Success {
		t.Fatalf("应成功: %+v", res)
	}
	if len(res.Attempts) != 1 || res.Attempts[0].StatusCode != http.StatusOK {
		t.Fatalf("应仅尝试 1 次且 200: %+v", res.Attempts)
	}
	if len(sl.Waits) != 0 {
		t.Fatalf("成功后不应退避: %v", sl.Waits)
	}
}

// flaky：前 2 次 500，第 3 次成功。验证重试、退避与最终成功。
func TestFlaky_RetriesAndSucceeds(t *testing.T) {
	clk, sl := newFakeDeps(time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC))
	srv := NewFakeServer(FaultSpec{Kind: FaultFlaky, FlakyTimes: 2})
	defer srv.Close()

	c := New(Config{
		MaxAttempts:    3,
		InitialBackoff: 10 * time.Millisecond,
		MaxBackoff:     100 * time.Millisecond,
		HTTPClient:     &http.Client{Timeout: 2 * time.Second},
	}, clk, sl)
	res := c.Get(srv.URL)

	if !res.Success {
		t.Fatalf("第 3 次应成功: %+v", res)
	}
	if len(res.Attempts) != 3 {
		t.Fatalf("应尝试 3 次，得到 %d", len(res.Attempts))
	}
	if res.Attempts[0].StatusCode != 500 || res.Attempts[2].StatusCode != 200 {
		t.Fatalf("尝试状态码序列错误: %+v", res.Attempts)
	}
	if !res.Attempts[0].Retried || !res.Attempts[1].Retried || res.Attempts[2].Retried {
		t.Fatalf("重试标记错误: %+v", res.Attempts)
	}
	// 指数退避：10ms、20ms。
	if len(sl.Waits) != 2 || sl.Waits[0] != 10*time.Millisecond || sl.Waits[1] != 20*time.Millisecond {
		t.Fatalf("退避序列应为 10ms,20ms，得到 %v", sl.Waits)
	}
	// 假时钟记录的耗时应等于退避总和 30ms。
	if res.ElapsedMS != 30 {
		t.Fatalf("假时钟耗时应为 30ms，得到 %d", res.ElapsedMS)
	}
}

// 持续 500 且尝试次数耗尽：失败并结构化记录每次尝试。
func TestStatus_ExhaustsAttempts(t *testing.T) {
	clk, sl := newFakeDeps(time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC))
	srv := NewFakeServer(FaultSpec{Kind: FaultStatus, StatusCode: 503})
	defer srv.Close()

	c := New(Config{
		MaxAttempts:    2,
		InitialBackoff: 5 * time.Millisecond,
		HTTPClient:     &http.Client{Timeout: 2 * time.Second},
	}, clk, sl)
	res := c.Get(srv.URL)

	if res.Success {
		t.Fatal("应失败")
	}
	if res.FinalStatus != 503 || len(res.Attempts) != 2 {
		t.Fatalf("结果错误: %+v", res)
	}
	if res.Error == "" {
		t.Fatal("失败时必须给出错误信息")
	}
}

// 坏响应体：200 但 JSON 截断，应被识别为错误并重试。
func TestBadBody_TreatedAsFailure(t *testing.T) {
	clk, sl := newFakeDeps(time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC))
	srv := NewFakeServer(FaultSpec{Kind: FaultBadBody})
	defer srv.Close()

	c := New(Config{
		MaxAttempts:    1,
		InitialBackoff: 5 * time.Millisecond,
		HTTPClient:     &http.Client{Timeout: 2 * time.Second},
	}, clk, sl)
	res := c.Get(srv.URL)

	if res.Success {
		t.Fatal("截断 JSON 不应判成功")
	}
	if res.Attempts[0].Error == "" {
		t.Fatalf("应记录响应体解码错误: %+v", res.Attempts[0])
	}
}

// 连接被直接关闭：应产生传输错误并结构化记录。
func TestConnectionClose_RecordedAsError(t *testing.T) {
	clk, sl := newFakeDeps(time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC))
	srv := NewFakeServer(FaultSpec{Kind: FaultClose})
	defer srv.Close()

	c := New(Config{
		MaxAttempts:    1,
		InitialBackoff: 5 * time.Millisecond,
		HTTPClient:     &http.Client{Timeout: 2 * time.Second},
	}, clk, sl)
	res := c.Get(srv.URL)

	if res.Success {
		t.Fatal("连接被关闭不应成功")
	}
	if res.Attempts[0].Error == "" {
		t.Fatalf("应记录连接错误: %+v", res.Attempts[0])
	}
}

// 4xx 不重试。
func TestClientErrorNotRetried(t *testing.T) {
	clk, sl := newFakeDeps(time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC))
	srv := NewFakeServer(FaultSpec{Kind: FaultStatus, StatusCode: 404})
	defer srv.Close()

	c := New(Config{MaxAttempts: 3, InitialBackoff: time.Millisecond,
		HTTPClient: &http.Client{Timeout: 2 * time.Second}}, clk, sl)
	res := c.Get(srv.URL)
	if res.Success || len(res.Attempts) != 1 {
		t.Fatalf("404 不应重试: %+v", res)
	}
}

func TestBackoffCapRespected(t *testing.T) {
	clk, sl := newFakeDeps(time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC))
	srv := NewFakeServer(FaultSpec{Kind: FaultStatus, StatusCode: 500})
	defer srv.Close()

	c := New(Config{
		MaxAttempts:    4,
		InitialBackoff: 100 * time.Millisecond,
		MaxBackoff:     150 * time.Millisecond,
		HTTPClient:     &http.Client{Timeout: 2 * time.Second},
	}, clk, sl)
	res := c.Get(srv.URL)
	_ = res
	// 100 -> 200 截断为 150 -> 300 截断为 150
	want := []time.Duration{100 * time.Millisecond, 150 * time.Millisecond, 150 * time.Millisecond}
	if len(sl.Waits) != 3 {
		t.Fatalf("应有 3 次退避，得到 %v", sl.Waits)
	}
	for i := range want {
		if sl.Waits[i] != want[i] {
			t.Fatalf("第 %d 次退避应为 %v，得到 %v", i, want[i], sl.Waits[i])
		}
	}
}
