package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// newTestServer 用默认示例方法启动一个测试服务器。
func newTestServer(t *testing.T, maxConcurrency int) *httptest.Server {
	t.Helper()
	gw := NewGateway(maxConcurrency)
	registerBuiltinMethods(gw)
	srv := httptest.NewServer(gw)
	t.Cleanup(srv.Close)
	return srv
}

func post(t *testing.T, url, body string) *http.Response {
	t.Helper()
	resp, err := http.Post(url, "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatalf("POST failed: %v", err)
	}
	return resp
}

func decodeBody(t *testing.T, resp *http.Response, v any) {
	t.Helper()
	defer resp.Body.Close()
	if err := json.NewDecoder(resp.Body).Decode(v); err != nil {
		t.Fatalf("decode response: %v", err)
	}
}

// responsesByID 把响应数组按 ID 原始字节建索引，断言不依赖响应顺序。
func responsesByID(t *testing.T, body string) map[string]Response {
	t.Helper()
	var rs []Response
	if err := json.Unmarshal([]byte(body), &rs); err != nil {
		t.Fatalf("response is not a JSON array: %v\nbody: %s", err, body)
	}
	m := make(map[string]Response, len(rs))
	for _, r := range rs {
		m[string(r.ID)] = r
	}
	return m
}

func readBodyString(t *testing.T, resp *http.Response) string {
	t.Helper()
	defer resp.Body.Close()
	var sb strings.Builder
	buf := make([]byte, 4096)
	for {
		n, err := resp.Body.Read(buf)
		sb.Write(buf[:n])
		if err != nil {
			break
		}
	}
	return sb.String()
}

func TestSingleRequestNumberID(t *testing.T) {
	srv := newTestServer(t, 8)
	resp := post(t, srv.URL, `{"jsonrpc":"2.0","method":"add","params":[1,2,3],"id":1}`)
	var r Response
	decodeBody(t, resp, &r)

	if r.Error != nil {
		t.Fatalf("unexpected error: %+v", r.Error)
	}
	if string(r.ID) != "1" {
		t.Fatalf("id not preserved, got %s", r.ID)
	}
	if r.Result == nil || string(*r.Result) != "6" {
		t.Fatalf("want result 6, got %v", r.Result)
	}
}

func TestSingleRequestStringID(t *testing.T) {
	srv := newTestServer(t, 8)
	resp := post(t, srv.URL, `{"jsonrpc":"2.0","method":"echo","params":{"a":1},"id":"abc-123"}`)
	var r Response
	decodeBody(t, resp, &r)

	if string(r.ID) != `"abc-123"` {
		t.Fatalf("string id not preserved, got %s", r.ID)
	}
	if r.Result == nil || string(*r.Result) != `{"a":1}` {
		t.Fatalf("echo mismatch: %v", r.Result)
	}
}

// 字符串 ID "1" 与数字 ID 1 必须可区分。
func TestStringAndNumberIDsAreDistinct(t *testing.T) {
	srv := newTestServer(t, 8)
	resp := post(t, srv.URL, `[
		{"jsonrpc":"2.0","method":"echo","params":["num"],"id":1},
		{"jsonrpc":"2.0","method":"echo","params":["str"],"id":"1"}
	]`)
	body := readBodyString(t, resp)
	m := responsesByID(t, body)

	if len(m) != 2 {
		t.Fatalf("want 2 distinct IDs, got %d: %s", len(m), body)
	}
	if got := string(*m["1"].Result); got != `["num"]` {
		t.Fatalf("numeric id result = %s", got)
	}
	if got := string(*m[`"1"`].Result); got != `["str"]` {
		t.Fatalf("string id result = %s", got)
	}
}

func TestParseError(t *testing.T) {
	srv := newTestServer(t, 8)
	resp := post(t, srv.URL, `{"jsonrpc":"2.0","method":`)
	var r Response
	decodeBody(t, resp, &r)

	if r.Error == nil || r.Error.Code != codeParseError {
		t.Fatalf("want -32700, got %+v", r.Error)
	}
	if string(r.ID) != "null" {
		t.Fatalf("parse error id must be null, got %s", r.ID)
	}
}

// 合法 JSON 但结构不合法 → -32600，必须与解析错误区分。
func TestInvalidRequestVsParseError(t *testing.T) {
	srv := newTestServer(t, 8)

	cases := []string{
		`{"method":"echo","id":1}`,                    // 缺 jsonrpc
		`{"jsonrpc":"2.0","id":1}`,                    // 缺 method
		`{"jsonrpc":"1.0","method":"echo","id":1}`,    // 版本错误
		`{"jsonrpc":"2.0","method":"echo","id":true}`, // 非法 ID 类型
		`42`, // 不是对象
		`"hello"`,
		`null`,
	}
	for _, body := range cases {
		resp := post(t, srv.URL, body)
		var r Response
		decodeBody(t, resp, &r)
		if r.Error == nil || r.Error.Code != codeInvalidRequest {
			t.Fatalf("body %s: want -32600, got %+v", body, r.Error)
		}
	}
}

// 空数组 → 单个 Invalid Request 错误对象（不是数组）。
func TestEmptyBatch(t *testing.T) {
	srv := newTestServer(t, 8)
	resp := post(t, srv.URL, `[]`)
	body := readBodyString(t, resp)

	var r Response
	if err := json.Unmarshal([]byte(body), &r); err != nil {
		t.Fatalf("empty batch must return a single error object, got: %s", body)
	}
	if r.Error == nil || r.Error.Code != codeInvalidRequest {
		t.Fatalf("want -32600, got %s", body)
	}
}

// 全通知批次 → 无任何响应内容。
func TestAllNotificationBatch(t *testing.T) {
	srv := newTestServer(t, 8)
	resp := post(t, srv.URL, `[
		{"jsonrpc":"2.0","method":"echo","params":[1]},
		{"jsonrpc":"2.0","method":"echo","params":[2]},
		{"jsonrpc":"2.0","method":"no-such-method"}
	]`)
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("want 204, got %d", resp.StatusCode)
	}
	body := readBodyString(t, resp)
	if strings.TrimSpace(body) != "" {
		t.Fatalf("notification batch must have empty body, got %q", body)
	}
}

// 单条通知也不产生响应。
func TestSingleNotification(t *testing.T) {
	srv := newTestServer(t, 8)
	resp := post(t, srv.URL, `{"jsonrpc":"2.0","method":"echo","params":["hi"]}`)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("want 204, got %d", resp.StatusCode)
	}
}

// 混合合法/非法项的批次：按 ID 映射校验，不依赖顺序。
func TestMixedBatch(t *testing.T) {
	srv := newTestServer(t, 8)
	resp := post(t, srv.URL, `[
		{"jsonrpc":"2.0","method":"add","params":[1,2],"id":1},
		{"jsonrpc":"2.0","method":"echo","params":["notify"]},
		{"jsonrpc":"2.0","method":"no-such","id":"x"},
		{"foo":"bar"},
		7,
		{"jsonrpc":"2.0","method":"fail","id":2}
	]`)
	body := readBodyString(t, resp)

	var rs []Response
	if err := json.Unmarshal([]byte(body), &rs); err != nil {
		t.Fatalf("response is not a JSON array: %v\nbody: %s", err, body)
	}
	// 通知不产生响应：6 项输入 → 5 个响应。
	if len(rs) != 5 {
		t.Fatalf("want 5 responses (notification excluded), got %d: %s", len(rs), body)
	}

	// 按 ID 建索引（null ID 单独统计，因为它们会互相覆盖）。
	m := make(map[string]Response)
	nulls := 0
	for _, r := range rs {
		if string(r.ID) == "null" {
			nulls++
			if r.Error == nil || r.Error.Code != codeInvalidRequest {
				t.Fatalf("invalid item: want -32600, got %+v", r.Error)
			}
			continue
		}
		m[string(r.ID)] = r
	}

	// id=1：成功。
	r1 := m["1"]
	if r1.Error != nil || r1.Result == nil || string(*r1.Result) != "3" {
		t.Fatalf("id=1: want result 3, got %+v", r1)
	}

	// id="x"：方法不存在 -32601。
	rx := m[`"x"`]
	if rx.Error == nil || rx.Error.Code != codeMethodNotFound {
		t.Fatalf(`id="x": want -32601, got %+v`, rx.Error)
	}

	// id=2：方法内错误 -32000。
	r2 := m["2"]
	if r2.Error == nil || r2.Error.Code != codeServerError {
		t.Fatalf("id=2: want -32000, got %+v", r2.Error)
	}

	// 两个非法项（{"foo":"bar"} 和 7）→ -32600，ID 为 null。
	if nulls != 2 {
		t.Fatalf("want 2 null-id error responses, got %d: %s", nulls, body)
	}
}

// 并发执行时响应按完成顺序返回，不要求与输入顺序一致；
// 用 ID 映射校验每一项都正确对应。
func TestOutOfOrderCompletion(t *testing.T) {
	srv := newTestServer(t, 8)
	// slow 在前但耗时 300ms，echo 在后但立即完成。
	resp := post(t, srv.URL, `[
		{"jsonrpc":"2.0","method":"slow","params":{"ms":300},"id":"slow"},
		{"jsonrpc":"2.0","method":"echo","params":["fast"],"id":"fast"}
	]`)
	body := readBodyString(t, resp)

	var rs []Response
	if err := json.Unmarshal([]byte(body), &rs); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(rs) != 2 {
		t.Fatalf("want 2 responses, got %d", len(rs))
	}

	// 乱序完成：快的应先出现在响应数组里。
	if string(rs[0].ID) != `"fast"` {
		t.Fatalf("expected fast call to complete first, got order: %s", body)
	}

	// 无论顺序如何，ID → 结果映射必须正确。
	m := responsesByID(t, body)
	if string(*m[`"slow"`].Result) != `{"slept_ms":300}` {
		t.Fatalf("slow result wrong: %s", body)
	}
	if string(*m[`"fast"`].Result) != `["fast"]` {
		t.Fatalf("fast result wrong: %s", body)
	}
}

// 并发上限：同时执行的方法数不得超过 maxConcurrency。
func TestConcurrencyLimit(t *testing.T) {
	const limit = 2
	gw := NewGateway(limit)

	var inFlight, maxSeen atomic.Int64
	gw.Register("track", func(_ context.Context, _ json.RawMessage) (json.RawMessage, *RPCError) {
		cur := inFlight.Add(1)
		for {
			old := maxSeen.Load()
			if cur <= old || maxSeen.CompareAndSwap(old, cur) {
				break
			}
		}
		time.Sleep(50 * time.Millisecond)
		inFlight.Add(-1)
		return json.RawMessage(`"done"`), nil
	})

	srv := httptest.NewServer(gw)
	defer srv.Close()

	resp := post(t, srv.URL, `[
		{"jsonrpc":"2.0","method":"track","id":1},
		{"jsonrpc":"2.0","method":"track","id":2},
		{"jsonrpc":"2.0","method":"track","id":3},
		{"jsonrpc":"2.0","method":"track","id":4},
		{"jsonrpc":"2.0","method":"track","id":5}
	]`)
	body := readBodyString(t, resp)
	m := responsesByID(t, body)
	if len(m) != 5 {
		t.Fatalf("want 5 responses, got %d: %s", len(m), body)
	}
	for id, r := range m {
		if r.Error != nil {
			t.Fatalf("id %s: unexpected error %+v", id, r.Error)
		}
	}

	if got := maxSeen.Load(); got > limit {
		t.Fatalf("concurrency limit violated: saw %d in flight, limit %d", got, limit)
	}
	if got := maxSeen.Load(); got < limit {
		t.Fatalf("expected calls to actually run in parallel up to %d, max seen %d", limit, got)
	}
}

func TestInvalidParams(t *testing.T) {
	srv := newTestServer(t, 8)
	resp := post(t, srv.URL, `{"jsonrpc":"2.0","method":"add","params":{"a":1},"id":9}`)
	var r Response
	decodeBody(t, resp, &r)
	if r.Error == nil || r.Error.Code != codeInvalidParams {
		t.Fatalf("want -32602, got %+v", r.Error)
	}
	if string(r.ID) != "9" {
		t.Fatalf("id not preserved on error, got %s", r.ID)
	}
}

// 通知中的错误（如方法不存在）同样不产生响应。
func TestNotificationWithUnknownMethod(t *testing.T) {
	srv := newTestServer(t, 8)
	resp := post(t, srv.URL, `{"jsonrpc":"2.0","method":"no-such"}`)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("want 204, got %d", resp.StatusCode)
	}
}
