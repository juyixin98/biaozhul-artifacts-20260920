package main

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"time"
)

// Resp 是客户端收到的一次 HTTP 交互结果。
//
// TransportError 非空表示连接层面失败（超时、对端关闭、连接重置）——
// 即调用方"没有拿到响应"，无法判断服务端是否已提交。这正是断线重试场景。
type Resp struct {
	StatusCode     int               `json:"status_code"`
	Headers        map[string]string `json:"headers,omitempty"`
	Body           string            `json:"body,omitempty"`
	TransportError string            `json:"transport_error,omitempty"`
	ElapsedMS      int64             `json:"elapsed_ms"`
}

// OK 表示拿到了 HTTP 响应（无论状态码）。
func (r Resp) OK() bool { return r.TransportError == "" && r.StatusCode > 0 }

// IsDisconnect 报告这是否为"对端无响应直接断开"类故障。
func (r Resp) IsDisconnect() bool { return r.TransportError != "" }

func newHTTPClient(timeout time.Duration) *http.Client {
	return &http.Client{Timeout: timeout}
}

// doRequest 执行一次请求并把结果（含传输层失败）结构化为 Resp。
//
// 请求体使用不可重绕的 oneShotReader：Go 的 http.Client 默认会在连接被对端
// 关闭且正文可重放时自动重发 POST；验收需要观察"第一次调用的原始断线"，
// 因此显式禁用自动重放，真正的重试逻辑由场景代码（waitForFinal）完成。
func doRequest(ctx context.Context, c *http.Client, method, url string, headers map[string]string, body []byte) Resp {
	var reader io.Reader
	if body != nil {
		reader = &oneShotReader{r: bytes.NewReader(body)}
	}
	req, err := http.NewRequestWithContext(ctx, method, url, reader)
	if err != nil {
		return Resp{TransportError: "build request: " + err.Error()}
	}
	// 双保险：不向 Transport 提供 GetBody，确保它不能重放请求。
	req.GetBody = nil
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	start := time.Now()
	resp, err := c.Do(req)
	elapsed := time.Since(start).Milliseconds()
	if err != nil {
		return Resp{TransportError: classifyTransportErr(err), ElapsedMS: elapsed}
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	hs := map[string]string{}
	for _, k := range []string{"Idempotency-Status", "Idempotency-Generation", "Retry-After", "Content-Type"} {
		if v := resp.Header.Get(k); v != "" {
			hs[k] = v
		}
	}
	return Resp{StatusCode: resp.StatusCode, Headers: hs, Body: string(b), ElapsedMS: elapsed}
}

// classifyTransportErr 把底层错误归类为可读标签。
func classifyTransportErr(err error) string {
	if err == nil {
		return ""
	}
	// url.Error / net.OpError 等统一以短文本呈现；EOF/reset 是断线的典型特征。
	return "transport: " + compactErr(err)
}

func compactErr(err error) string {
	s := err.Error()
	const max = 160
	if len(s) > max {
		s = s[:max] + "..."
	}
	return s
}

// oneShotReader 包装一个 reader 但不暴露 Seek/重放能力，
// 使 net/http 无法在连接断开时自动重试该请求。
type oneShotReader struct{ r io.Reader }

func (o *oneShotReader) Read(p []byte) (int, error) { return o.r.Read(p) }
