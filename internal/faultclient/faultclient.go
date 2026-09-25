// Package faultclient 提供一个带重试与退避的 HTTP 客户端，
// 以及一个同进程的假 HTTP 服务（httptest），用于在不接触生产系统的前提下
// 演练超时、连接错误、坏状态码、坏响应体等故障。
package faultclient

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"time"

	"contractcheck/internal/clock"
)

// FaultKind 是假服务可注入的故障类型。
type FaultKind string

const (
	FaultNone    FaultKind = "none"    // 正常 200 + JSON
	FaultDelay   FaultKind = "delay"   // 服务端延迟
	FaultStatus  FaultKind = "status"  // 返回指定（错误）状态码
	FaultBadBody FaultKind = "badbody" // 返回截断的非法 JSON 后关闭连接
	FaultClose   FaultKind = "close"   // 接受连接后直接断开，不回响应
	FaultFlaky   FaultKind = "flaky"   // 前 N 次 500，之后恢复 200
)

// FaultSpec 描述假服务的故障注入配置。
type FaultSpec struct {
	Kind       FaultKind     `json:"kind"`
	Delay      time.Duration `json:"-"`          // FaultDelay 的延迟
	StatusCode int           `json:"statusCode"` // FaultStatus 的状态码
	FlakyTimes int           `json:"flakyTimes"` // FaultFlaky 前几次失败
	// RealServerSleep 为 true 时 delay 故障真的占用真实时间（默认假睡，便于快速测试）。
	RealServerSleep bool `json:"-"`
}

// Sleeper 抽象退避等待，测试时可用立即返回的假实现。
type Sleeper interface {
	Sleep(d time.Duration)
}

// RealSleeper 使用真实时间。
type RealSleeper struct{}

// Sleep 等待真实时长 d。
func (RealSleeper) Sleep(d time.Duration) { time.Sleep(d) }

// FakeSleeper 记录退避调用并立即返回，可选联动推进假时钟。
type FakeSleeper struct {
	clk       *clock.Fake
	Waits     []time.Duration
	callCount int32
}

// NewFakeSleeper 创建假睡器；clk 非 nil 时每次 Sleep 同步推进假时钟。
func NewFakeSleeper(clk *clock.Fake) *FakeSleeper { return &FakeSleeper{clk: clk} }

// Sleep 立即返回，并把等待时长记入 Waits / 推进假时钟。
func (f *FakeSleeper) Sleep(d time.Duration) {
	f.Waits = append(f.Waits, d)
	atomic.AddInt32(&f.callCount, 1)
	if f.clk != nil {
		f.clk.Advance(d)
	}
}

// Attempt 记录单次请求尝试。
type Attempt struct {
	Index      int       `json:"index"`
	StartedAt  time.Time `json:"startedAt"`
	StatusCode int       `json:"statusCode,omitempty"`
	LatencyMS  int64     `json:"latencyMs"`
	Error      string    `json:"error,omitempty"`
	Retried    bool      `json:"retried"`
}

// CallResult 是一次（含重试的）调用的结构化结果。
type CallResult struct {
	URL         string    `json:"url"`
	Success     bool      `json:"success"`
	FinalStatus int       `json:"finalStatus,omitempty"`
	Attempts    []Attempt `json:"attempts"`
	ElapsedMS   int64     `json:"elapsedMs"`
	Error       string    `json:"error,omitempty"`
}

// Config 是客户端配置。
type Config struct {
	MaxAttempts    int           // 含首次的总尝试次数
	InitialBackoff time.Duration // 首次退避，之后指数翻倍
	MaxBackoff     time.Duration
	HTTPClient     *http.Client // 默认使用带超时的客户端
}

// Client 是带指数退避重试的故障演练客户端。
type Client struct {
	cfg     Config
	clk     clock.Clock
	sleeper Sleeper
	hc      *http.Client
}

// New 创建客户端。clk 提供时间戳，sleeper 提供退避等待（均可控）。
func New(cfg Config, clk clock.Clock, sleeper Sleeper) *Client {
	if cfg.MaxAttempts < 1 {
		cfg.MaxAttempts = 1
	}
	if cfg.InitialBackoff <= 0 {
		cfg.InitialBackoff = 10 * time.Millisecond
	}
	if cfg.MaxBackoff <= 0 {
		cfg.MaxBackoff = time.Second
	}
	hc := cfg.HTTPClient
	if hc == nil {
		hc = &http.Client{Timeout: 2 * time.Second}
	}
	return &Client{cfg: cfg, clk: clk, sleeper: sleeper, hc: hc}
}

// Get 对 baseURL+path 发起 GET，按配置重试，返回结构化结果。
func (c *Client) Get(rawURL string) *CallResult {
	start := c.clk.Now()
	res := &CallResult{URL: rawURL}

	var lastStatus int
	var lastErr string
	for attempt := 1; attempt <= c.cfg.MaxAttempts; attempt++ {
		a := Attempt{Index: attempt, StartedAt: c.clk.Now().UTC()}
		resp, err := c.hc.Get(rawURL)
		if err != nil {
			a.Error = err.Error()
			lastErr = err.Error()
		} else {
			a.StatusCode = resp.StatusCode
			lastStatus = resp.StatusCode
			// 完整读取以暴露坏响应体（截断 JSON 在解码时失败）。
			if resp.StatusCode == http.StatusOK {
				var body map[string]any
				if decErr := json.NewDecoder(resp.Body).Decode(&body); decErr != nil {
					a.Error = "响应体不是合法 JSON: " + decErr.Error()
					lastErr = a.Error
				}
			}
			resp.Body.Close()
		}
		a.LatencyMS = c.clk.Now().Sub(a.StartedAt).Milliseconds()

		shouldRetry := attempt < c.cfg.MaxAttempts &&
			(err != nil || isRetriableStatus(lastStatus) || (resp != nil && a.Error != "" && lastStatus == http.StatusOK))
		a.Retried = shouldRetry
		res.Attempts = append(res.Attempts, a)
		resp = nil
		if !shouldRetry {
			break
		}
		c.sleeper.Sleep(c.backoff(attempt))
	}

	res.ElapsedMS = c.clk.Now().Sub(start).Milliseconds()
	res.FinalStatus = lastStatus
	res.Success = lastErr == "" && lastStatus >= 200 && lastStatus < 300
	if !res.Success && lastErr == "" {
		lastErr = fmt.Sprintf("调用失败：最终状态码 %d", lastStatus)
	}
	if !res.Success {
		res.Error = lastErr
	}
	return res
}

func isRetriableStatus(status int) bool {
	return status == http.StatusTooManyRequests || status >= 500
}

func (c *Client) backoff(attempt int) time.Duration {
	d := c.cfg.InitialBackoff
	for i := 1; i < attempt; i++ {
		d *= 2
		if d >= c.cfg.MaxBackoff {
			return c.cfg.MaxBackoff
		}
	}
	if d > c.cfg.MaxBackoff {
		return c.cfg.MaxBackoff
	}
	return d
}

// NewFakeServer 启动一个按 spec 注入故障的进程内假服务，返回其 *httptest.Server。
// 调用方负责 server.Close()。
func NewFakeServer(spec FaultSpec) *httptest.Server {
	var hits int32
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch spec.Kind {
		case FaultDelay:
			if spec.RealServerSleep {
				time.Sleep(spec.Delay)
			}
			writeJSON(w, http.StatusOK, map[string]any{"ok": true, "servedAt": time.Now().UTC()})
		case FaultStatus:
			code := spec.StatusCode
			if code == 0 {
				code = http.StatusInternalServerError
			}
			w.WriteHeader(code)
			_, _ = w.Write([]byte(`{"error":"injected"}`))
		case FaultBadBody:
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"ok": tru`)) // 截断的非法 JSON
		case FaultClose:
			// 劫持连接后直接关闭，不发送任何 HTTP 响应。
			hj, ok := w.(http.Hijacker)
			if ok {
				conn, _, _ := hj.Hijack()
				_ = conn.Close()
			}
		case FaultFlaky:
			n := atomic.AddInt32(&hits, 1)
			if int(n) <= spec.FlakyTimes {
				w.WriteHeader(http.StatusInternalServerError)
				_, _ = w.Write([]byte(`{"error":"flaky"}`))
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{"ok": true, "attempt": n})
		default:
			writeJSON(w, http.StatusOK, map[string]any{"ok": true})
		}
	}))
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
