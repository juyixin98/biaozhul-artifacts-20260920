// Package inference 提供按兼容键合批的“推理请求模拟器”：
// 请求与响应的结构仿照常见推理服务，执行器不依赖真实模型，
// 只做确定性的延迟与结果/失败模拟。
package inference

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
	"sync"
	"time"

	"batchagg/batch"
)

// Request 是单个推理请求。兼容键（Key）为模型名：
// 只有发往同一模型的请求才会被合到同一批次。
type Request struct {
	// ID 由调用方提供（可为空，由 HTTP 层分配），用于结果关联。
	ID string `json:"id,omitempty"`
	// Model 是合批兼容键，也是模拟推理的“模型名”。
	Model string `json:"model"`
	// Prompt 为提示词；其长度参与字节计数。
	Prompt string `json:"prompt"`
	// SimulateError 非空时，该请求在批中返回失败（用于部分失败演示）。
	// 常见取值：model_error、rate_limited。
	SimulateError string `json:"simulate_error,omitempty"`
}

// Key 实现 batch.Request：同模型才可合批。
func (r *Request) Key() string { return r.Model }

// SchedulerID 返回调用方指定的请求 ID。非空时调度器事件直接使用该 ID，
// 便于 HTTP 响应与 SSE 事件关联；为空时由调度器自动生成。
func (r *Request) SchedulerID() string { return r.ID }

// Size 返回请求的逻辑字节数（提示词 UTF-8 字节 + 固定头部开销）。
func (r *Request) Size() int {
	const overhead = 64
	return overhead + len(r.Model) + len(r.Prompt)
}

// Response 是单请求的推理结果。
type Response struct {
	ID        string `json:"id"`
	Model     string `json:"model"`
	Output    string `json:"output"`
	EchoLen   int    `json:"echo_len"`
	ServedBy  string `json:"served_by"`
	BatchSeq  int64  `json:"batch_seq"`
	BatchSize int    `json:"batch_size"`
	DelayMS   int64  `json:"delay_ms"`
}

// ItemError 描述单个请求的失败（部分失败的粒度）。
type ItemError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (e *ItemError) Error() string { return fmt.Sprintf("%s: %s", e.Code, e.Message) }

var errorMessages = map[string]string{
	"model_error":    "simulated model-side failure",
	"rate_limited":   "simulated rate limit",
	"content_policy": "simulated content policy rejection",
}

// FakeExecutor 是确定性模拟执行器：
//   - 对一批请求 Sleep Latency（支持测试时钟下的 sleep 回调替换）；
//   - 逐项产出结果或失败，互不影响；
//   - 记录执行过的批次数，供测试断言。
type FakeExecutor struct {
	// Latency 为每批的模拟处理延迟。为 0 表示不等待。
	Latency time.Duration
	// Sleep 可注入测试用等待（如虚拟时钟）；nil 时使用 time.Sleep。
	Sleep func(d time.Duration)

	mu        sync.Mutex
	batchSeq  int64
	batchSeen []int
}

// Execute 实现 batch.Executor。
func (e *FakeExecutor) Execute(_ context.Context, items []*Request) []batch.ItemResult {
	if e.Sleep != nil {
		e.Sleep(e.Latency)
	} else {
		time.Sleep(e.Latency)
	}

	e.mu.Lock()
	e.batchSeq++
	seq := e.batchSeq
	e.batchSeen = append(e.batchSeen, len(items))
	e.mu.Unlock()

	results := make([]batch.ItemResult, len(items))
	for i, req := range items {
		if req.SimulateError != "" {
			code := req.SimulateError
			msg, ok := errorMessages[code]
			if !ok {
				msg = "simulated failure"
			}
			results[i].Err = &ItemError{Code: code, Message: msg}
			continue
		}
		results[i].Output = map[string]any{
			"id":         req.ID,
			"model":      req.Model,
			"output":     mockCompletion(req),
			"echo_len":   len([]rune(req.Prompt)),
			"served_by":  "fake-executor",
			"batch_seq":  seq,
			"batch_size": len(items),
			"delay_ms":   e.Latency.Milliseconds(),
		}
	}
	return results
}

// BatchCount 返回已执行批次数。
func (e *FakeExecutor) BatchCount() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return len(e.batchSeen)
}

// BatchSizes 返回每次执行的批大小（按执行顺序）。
func (e *FakeExecutor) BatchSizes() []int {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := make([]int, len(e.batchSeen))
	copy(out, e.batchSeen)
	return out
}

func mockCompletion(r *Request) string {
	h := sha256.Sum256([]byte(r.Model + "\x00" + r.Prompt))
	return "sim:" + strings.ToUpper(hex.EncodeToString(h[:8]))
}
