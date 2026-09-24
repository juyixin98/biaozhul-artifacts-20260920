package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// ---- test helpers ----------------------------------------------------------

func newTestGateway(t *testing.T, cfg Config) (*Gateway, *httptest.Server) {
	t.Helper()
	gw := NewGateway(cfg)
	gw.registerBuiltins()
	srv := httptest.NewServer(gw)
	t.Cleanup(srv.Close)
	return gw, srv
}

func postRaw(t *testing.T, url, body, contentType string) (int, []byte) {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, url, strings.NewReader(body))
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("http post: %v", err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, data
}

func postJSON(t *testing.T, url, body string) (int, []byte) {
	t.Helper()
	return postRaw(t, url, body, "application/json")
}

// decodeUseNumberBytes mirrors the server's number-preserving decode.
func decodeUseNumberBytes(t *testing.T, b []byte) interface{} {
	t.Helper()
	var v interface{}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	if err := dec.Decode(&v); err != nil {
		t.Fatalf("decode response %q: %v", string(b), err)
	}
	return v
}

func asMap(t *testing.T, b []byte) map[string]interface{} {
	t.Helper()
	v, ok := decodeUseNumberBytes(t, b).(map[string]interface{})
	if !ok {
		t.Fatalf("expected JSON object, got: %s", string(b))
	}
	return v
}

func asArray(t *testing.T, b []byte) []interface{} {
	t.Helper()
	v, ok := decodeUseNumberBytes(t, b).([]interface{})
	if !ok {
		t.Fatalf("expected JSON array, got: %s", string(b))
	}
	return v
}

func respErrCode(m map[string]interface{}) int {
	e, ok := m["error"].(map[string]interface{})
	if !ok {
		return 0
	}
	c, _ := e["code"].(json.Number)
	n, _ := c.Int64()
	return int(n)
}

func respID(m map[string]interface{}) interface{} {
	return m["id"]
}

func respResult(m map[string]interface{}) interface{} {
	return m["result"]
}

func mustNumber(t *testing.T, v interface{}) json.Number {
	t.Helper()
	n, ok := v.(json.Number)
	if !ok {
		t.Fatalf("expected json.Number, got %T (%v)", v, v)
	}
	return n
}

// ---- single request / notification ----------------------------------------

func TestSingleRequest_StringID(t *testing.T) {
	_, srv := newTestGateway(t, Config{Concurrency: 4})
	status, body := postJSON(t, srv.URL, `{"jsonrpc":"2.0","method":"ping","id":"abc"}`)
	if status != http.StatusOK {
		t.Fatalf("status = %d, want 200", status)
	}
	m := asMap(t, body)
	if m["jsonrpc"] != "2.0" || respResult(m) != "pong" {
		t.Fatalf("unexpected response: %s", body)
	}
	if respID(m) != "abc" {
		t.Fatalf("id = %v, want \"abc\"", respID(m))
	}
}

func TestSingleRequest_NumericIDPreservedExactly(t *testing.T) {
	_, srv := newTestGateway(t, Config{Concurrency: 4})
	// Number larger than 2^53 must survive without float64 rounding.
	status, body := postJSON(t, srv.URL, `{"jsonrpc":"2.0","method":"ping","id":9007199254740993}`)
	if status != http.StatusOK {
		t.Fatalf("status = %d", status)
	}
	id := mustNumber(t, respID(asMap(t, body)))
	if id.String() != "9007199254740993" {
		t.Fatalf("id = %s, want 9007199254740993 (no rounding)", id)
	}
}

func TestStringAndNumericOneAreDistinctIDs(t *testing.T) {
	_, srv := newTestGateway(t, Config{Concurrency: 4})
	batch := `[
		{"jsonrpc":"2.0","method":"ping","id":1},
		{"jsonrpc":"2.0","method":"ping","id":"1"}
	]`
	status, body := postJSON(t, srv.URL, batch)
	if status != http.StatusOK {
		t.Fatalf("status = %d", status)
	}
	arr := asArray(t, body)
	if len(arr) != 2 {
		t.Fatalf("got %d responses, want 2", len(arr))
	}
	seen := map[string]bool{}
	for _, item := range arr {
		m := item.(map[string]interface{})
		switch id := m["id"].(type) {
		case json.Number:
			if id.String() != "1" {
				t.Fatalf("numeric id = %s", id)
			}
			seen["num"] = true
		case string:
			if id != "1" {
				t.Fatalf("string id = %s", id)
			}
			seen["str"] = true
		default:
			t.Fatalf("unexpected id type %T", id)
		}
	}
	if !seen["num"] || !seen["str"] {
		t.Fatalf("numeric and string id were not both preserved: %v", seen)
	}
}

func TestSingleNotification_NoContent(t *testing.T) {
	_, srv := newTestGateway(t, Config{Concurrency: 4})
	status, body := postJSON(t, srv.URL, `{"jsonrpc":"2.0","method":"ping"}`)
	if status != http.StatusNoContent {
		t.Fatalf("status = %d, want 204", status)
	}
	if len(body) != 0 {
		t.Fatalf("notification produced a body: %s", body)
	}
}

// ---- batches: acceptance cases ---------------------------------------------

func TestBatch_AllNotifications_NoContent(t *testing.T) {
	_, srv := newTestGateway(t, Config{Concurrency: 4})
	body := `[
		{"jsonrpc":"2.0","method":"ping"},
		{"jsonrpc":"2.0","method":"sum","params":[1,2,3]},
		{"jsonrpc":"2.0","method":"subtract","params":{"a":9,"b":5}}
	]`
	status, data := postJSON(t, srv.URL, body)
	if status != http.StatusNoContent {
		t.Fatalf("status = %d, want 204", status)
	}
	if len(data) != 0 {
		t.Fatalf("all-notification batch must not return data, got %s", data)
	}
}

func TestBatch_EmptyArray_InvalidRequest(t *testing.T) {
	_, srv := newTestGateway(t, Config{Concurrency: 4})
	status, body := postJSON(t, srv.URL, `[]`)
	if status != http.StatusOK {
		t.Fatalf("status = %d, want 200", status)
	}
	// Response must be a single error object, not an array.
	m := asMap(t, body)
	if code := respErrCode(m); code != codeInvalidRequest {
		t.Fatalf("code = %d, want -32600", code)
	}
	if id := respID(m); id != nil {
		t.Fatalf("id = %v, want null", id)
	}
}

func TestBatch_MixedValidInvalidAndNotifications(t *testing.T) {
	_, srv := newTestGateway(t, Config{Concurrency: 8})
	body := `[
		{"jsonrpc":"2.0","method":"sum","params":[1,2,3],"id":1},
		{"jsonrpc":"2.0","method":"ping"},
		{"jsonrpc":"2.0","method":"does.not.exist","id":"missing"},
		{"jsonrpc":"2.0","method":"sum","params":[1,"oops"],"id":9},
		{"foo":"bar"},
		null,
		42,
		{"jsonrpc":"2.0","method":"ping","id":"ok"},
		{"jsonrpc":"2.0","method":"nope"},
		{"jsonrpc":"2.0","method":"subtract","params":[10,4],"id":2}
	]`
	status, data := postJSON(t, srv.URL, body)
	if status != http.StatusOK {
		t.Fatalf("status = %d", status)
	}
	arr := asArray(t, data)

	// Notifications (2): plain ping, and unknown-method notification.
	// Responses expected for the other 8 items.
	if len(arr) != 8 {
		t.Fatalf("got %d responses, want 8 (2 notifications produce none): %s", len(arr), data)
	}

	byID := map[string]map[string]interface{}{}
	for _, item := range arr {
		m := item.(map[string]interface{})
		var key string
		switch id := m["id"].(type) {
		case json.Number:
			key = "n:" + id.String()
		case string:
			key = "s:" + id
		case nil:
			key = "null"
		default:
			t.Fatalf("unexpected id type %T", id)
		}
		byID[key] = m
	}

	// id 1 -> sum result 6
	r1, ok := byID["n:1"]
	if !ok {
		t.Fatalf("missing response for id 1: %v", byID)
	}
	if v := mustNumber(t, respResult(r1)).String(); v != "6" {
		t.Fatalf("sum result = %s, want 6", v)
	}
	if code := respErrCode(r1); code != 0 {
		t.Fatalf("id 1 unexpectedly errored: %v", r1["error"])
	}

	// id 2 -> subtract result 6
	r2 := byID["n:2"]
	if v := mustNumber(t, respResult(r2)).String(); v != "6" {
		t.Fatalf("subtract result = %v, want 6", respResult(r2))
	}

	// string id "missing" -> -32601, string id echoed
	rm := byID["s:missing"]
	if code := respErrCode(rm); code != codeMethodNotFound {
		t.Fatalf("missing-method code = %d, want -32601", code)
	}
	if respID(rm) != "missing" {
		t.Fatalf("error response lost its string id: %v", respID(rm))
	}

	// id 9 -> -32602 invalid params (request shape is valid, data is bad)
	r9 := byID["n:9"]
	if code := respErrCode(r9); code != codeInvalidParams {
		t.Fatalf("bad-element code = %d, want -32602", code)
	}

	// string id "ok" -> pong
	rok := byID["s:ok"]
	if respResult(rok) != "pong" {
		t.Fatalf("id \"ok\" result = %v, want pong", respResult(rok))
	}

	// The three malformed items -> exactly three -32600 responses with null id.
	invalid := 0
	for _, item := range arr {
		m := item.(map[string]interface{})
		if respErrCode(m) == codeInvalidRequest {
			if m["id"] != nil {
				t.Fatalf("invalid-request id must be null, got %v", m["id"])
			}
			invalid++
		}
	}
	if invalid != 3 {
		t.Fatalf("got %d invalid-request responses, want 3", invalid)
	}
}

func TestBatch_OutOfOrderCompletion(t *testing.T) {
	_, srv := newTestGateway(t, Config{Concurrency: 8})
	// Input order A,B,C with delays 300/100/200ms: completion order is B,C,A.
	body := `[
		{"jsonrpc":"2.0","method":"delay","params":[300],"id":"A"},
		{"jsonrpc":"2.0","method":"delay","params":[100],"id":"B"},
		{"jsonrpc":"2.0","method":"delay","params":[200],"id":"C"}
	]`
	start := time.Now()
	status, data := postJSON(t, srv.URL, body)
	elapsed := time.Since(start)
	if status != http.StatusOK {
		t.Fatalf("status = %d", status)
	}
	arr := asArray(t, data)
	if len(arr) != 3 {
		t.Fatalf("got %d responses, want 3", len(arr))
	}

	var order []string
	for _, item := range arr {
		m := item.(map[string]interface{})
		id, ok := m["id"].(string)
		if !ok {
			t.Fatalf("non-string id in out-of-order batch: %v", m["id"])
		}
		if respResult(m) != "done" {
			t.Fatalf("id %s result = %v, want done", id, respResult(m))
		}
		order = append(order, id)
	}
	want := []string{"B", "C", "A"}
	if strings.Join(order, ",") != strings.Join(want, ",") {
		t.Fatalf("completion order = %v, want %v (responses must not be input-sorted)", order, want)
	}
	// With real concurrency the three delays overlap; serial execution would be 600ms.
	if elapsed >= 500*time.Millisecond {
		t.Fatalf("batch took %v; delays did not run concurrently", elapsed)
	}
}

func TestBatch_ConcurrencyIsCapped(t *testing.T) {
	gw := NewGateway(Config{Concurrency: 4})
	var active, maxActive int64
	gw.Register("work", func(_ context.Context, _ json.RawMessage) (interface{}, error) {
		cur := atomic.AddInt64(&active, 1)
		for {
			peak := atomic.LoadInt64(&maxActive)
			if cur <= peak || atomic.CompareAndSwapInt64(&maxActive, peak, cur) {
				break
			}
		}
		time.Sleep(20 * time.Millisecond)
		atomic.AddInt64(&active, -1)
		return "ok", nil
	})
	srv := httptest.NewServer(gw)
	defer srv.Close()

	var sb strings.Builder
	sb.WriteByte('[')
	for i := 0; i < 40; i++ {
		if i > 0 {
			sb.WriteByte(',')
		}
		fmt.Fprintf(&sb, `{"jsonrpc":"2.0","method":"work","id":%d}`, i)
	}
	sb.WriteByte(']')

	status, data := postJSON(t, srv.URL, sb.String())
	if status != http.StatusOK {
		t.Fatalf("status = %d", status)
	}
	arr := asArray(t, data)
	if len(arr) != 40 {
		t.Fatalf("got %d responses, want 40", len(arr))
	}
	if peak := atomic.LoadInt64(&maxActive); peak > 4 {
		t.Fatalf("observed %d concurrent handlers, ceiling is 4", peak)
	}
	if peak := atomic.LoadInt64(&maxActive); peak < 2 {
		t.Fatalf("handlers never ran concurrently (peak %d)", peak)
	}
	// Every id 0..39 must map to a successful result.
	got := map[string]bool{}
	for _, item := range arr {
		m := item.(map[string]interface{})
		if code := respErrCode(m); code != 0 {
			t.Fatalf("unexpected error: %v", m["error"])
		}
		got[mustNumber(t, m["id"]).String()] = true
	}
	for i := 0; i < 40; i++ {
		if !got[fmt.Sprintf("%d", i)] {
			t.Fatalf("missing response id %d", i)
		}
	}
}

// ---- error classification --------------------------------------------------

func TestParseError_MalformedJSON(t *testing.T) {
	_, srv := newTestGateway(t, Config{Concurrency: 2})
	for _, body := range []string{
		`{"jsonrpc":`,
		``,
		`not json at all`,
		`{"jsonrpc":"2.0","method":"ping","id":1} extra`,
		`[{"jsonrpc":"2.0"} broken`,
	} {
		status, data := postJSON(t, srv.URL, body)
		if status != http.StatusOK {
			t.Fatalf("body=%q status=%d, want 200", body, status)
		}
		m := asMap(t, data)
		if code := respErrCode(m); code != codeParseError {
			t.Fatalf("body=%q code=%d, want -32700", body, code)
		}
	}
}

func TestInvalidRequest_VersusParseError(t *testing.T) {
	_, srv := newTestGateway(t, Config{Concurrency: 2})

	// Valid JSON but wrong shape -> Invalid Request, not Parse error.
	for _, tc := range []struct {
		body string
		code int
	}{
		{`42`, codeInvalidRequest},                                                  // top-level scalar
		{`"hello"`, codeInvalidRequest},                                             // top-level string
		{`{"foo":"bar"}`, codeInvalidRequest},                                       // missing jsonrpc/method
		{`{"jsonrpc":"1.0","method":"ping","id":1}`, codeInvalidRequest},            // bad version
		{`{"jsonrpc":"2.0","method":null,"id":1}`, codeInvalidRequest},              // null method
		{`{"jsonrpc":"2.0","method":"ping","params":7,"id":1}`, codeInvalidRequest}, // bad params
		{`{"jsonrpc":"2.0","method":"ping","id":[1]}`, codeInvalidRequest},          // array id
		{`{"jsonrpc":"2.0","method":"ping","id":{"x":1}}`, codeInvalidRequest},      // object id
	} {
		status, data := postJSON(t, srv.URL, tc.body)
		if status != http.StatusOK {
			t.Fatalf("body=%s status=%d", tc.body, status)
		}
		m := asMap(t, data)
		if code := respErrCode(m); code != tc.code {
			t.Fatalf("body=%s code=%d, want %d; resp=%s", tc.body, code, tc.code, data)
		}
		if m["id"] != nil {
			t.Fatalf("body=%s invalid-request id must be null, got %v", tc.body, m["id"])
		}
	}
}

func TestBatch_InvalidItemsEachGetNullIDError(t *testing.T) {
	_, srv := newTestGateway(t, Config{Concurrency: 2})
	body := `[1, "two", true, null, {"jsonrpc":"2.0","method":"ping","id":"good"}]`
	status, data := postJSON(t, srv.URL, body)
	if status != http.StatusOK {
		t.Fatalf("status=%d", status)
	}
	arr := asArray(t, data)
	if len(arr) != 5 {
		t.Fatalf("got %d responses, want 5", len(arr))
	}
	for i, item := range arr[:4] {
		m := item.(map[string]interface{})
		if code := respErrCode(m); code != codeInvalidRequest {
			t.Fatalf("item %d code=%d, want -32600", i, code)
		}
		if m["id"] != nil {
			t.Fatalf("item %d id=%v, want null", i, m["id"])
		}
	}
	last := arr[4].(map[string]interface{})
	if last["id"] != "good" || respResult(last) != "pong" {
		t.Fatalf("valid item corrupted: %v", last)
	}
}

// ---- HTTP semantics --------------------------------------------------------

func TestGETNotAllowed(t *testing.T) {
	_, srv := newTestGateway(t, Config{Concurrency: 2})
	resp, err := http.Get(srv.URL)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("GET status=%d, want 405", resp.StatusCode)
	}
	if allow := resp.Header.Get("Allow"); allow != "POST" {
		t.Fatalf("Allow header = %q, want POST", allow)
	}
}

func TestExplicitNullIDIsARequest(t *testing.T) {
	_, srv := newTestGateway(t, Config{Concurrency: 2})
	status, body := postJSON(t, srv.URL, `{"jsonrpc":"2.0","method":"missing","id":null}`)
	if status != http.StatusOK {
		t.Fatalf("status=%d", status)
	}
	m := asMap(t, body)
	if m["id"] != nil { // JSON null decodes to Go nil
		t.Fatalf("id should be JSON null")
	}
	if code := respErrCode(m); code != codeMethodNotFound {
		t.Fatalf("code=%d, want -32601; explicit null id is a request, not a notification", code)
	}
}

func TestNamedParamsSubtract(t *testing.T) {
	_, srv := newTestGateway(t, Config{Concurrency: 2})
	status, body := postJSON(t, srv.URL,
		`{"jsonrpc":"2.0","method":"subtract","params":{"a":42,"b":17},"id":"s"}`)
	if status != http.StatusOK {
		t.Fatalf("status=%d", status)
	}
	if v := mustNumber(t, respResult(asMap(t, body))).String(); v != "25" {
		t.Fatalf("subtract named = %s, want 25", v)
	}
}

func TestEchoPreservesNumericShape(t *testing.T) {
	_, srv := newTestGateway(t, Config{Concurrency: 2})
	body := `{"jsonrpc":"2.0","method":"echo","params":{"big":9007199254740993,"s":"x","f":1.5},"id":1}`
	status, data := postJSON(t, srv.URL, body)
	if status != http.StatusOK {
		t.Fatalf("status=%d", status)
	}
	m := asMap(t, data)
	res := respResult(m).(map[string]interface{})
	if n := mustNumber(t, res["big"]); n.String() != "9007199254740993" {
		t.Fatalf("echo rounded big number: %s", n)
	}
	if f := mustNumber(t, res["f"]); f.String() != "1.5" {
		t.Fatalf("echo f = %s", f)
	}
	if res["s"] != "x" {
		t.Fatalf("echo s = %v", res["s"])
	}
}
