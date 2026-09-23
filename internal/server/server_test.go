package server_test

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"respd/internal/kv"
	"respd/internal/resp"
	"respd/internal/server"
)

func newTestServer(t *testing.T, opts ...server.Option) (*httptest.Server, *server.Server) {
	t.Helper()
	srv := server.New(kv.New(), opts...)
	ts := httptest.NewServer(srv.Mux())
	t.Cleanup(func() {
		ts.Close()
		srv.Close()
	})
	return ts, srv
}

// ---------------------------------------------------------------------------
// independentEncoder: reference writer (string-based), kept separate from
// the production Encoder just like in the resp package tests.
// ---------------------------------------------------------------------------

type refEncoder struct{ b bytes.Buffer }

func (e *refEncoder) cmd(parts ...string) {
	fmt.Fprintf(&e.b, "*%d\r\n", len(parts))
	for _, p := range parts {
		fmt.Fprintf(&e.b, "$%d\r\n%s\r\n", len(p), p)
	}
}

func (e *refEncoder) raw(s string) { e.b.WriteString(s) }

// ---------------------------------------------------------------------------
// RESP client helpers over the raw /resp endpoint.
// ---------------------------------------------------------------------------

func doRaw(t *testing.T, ts *httptest.Server, body []byte, hdr map[string]string) (int, []byte) {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, ts.URL+"/resp", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	resp2, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	data, _ := io.ReadAll(resp2.Body)
	return resp2.StatusCode, data
}

func mustDecode(t *testing.T, data []byte) []resp.Value {
	t.Helper()
	dec := resp.NewDecoder(bytes.NewReader(data))
	var out []resp.Value
	for {
		v, err := dec.Next()
		if err == io.EOF {
			return out
		}
		if err != nil {
			t.Fatalf("decode reply %q: %v", data, err)
		}
		out = append(out, v)
	}
}

// ---------------------------------------------------------------------------
// basics + pipeline ordering
// ---------------------------------------------------------------------------

func TestPipelinePing(t *testing.T) {
	ts, _ := newTestServer(t)
	e := refEncoder{}
	e.cmd("PING")
	e.cmd("PING", "hello")
	e.cmd("ECHO", "abc")
	status, body := doRaw(t, ts, e.b.Bytes(), nil)
	if status != http.StatusOK {
		t.Fatalf("status %d: %q", status, body)
	}
	got := mustDecode(t, body)
	if len(got) != 3 {
		t.Fatalf("want 3 replies, got %d: %v", len(got), got)
	}
	if got[0].Kind != resp.KindSimple || got[0].Str != "PONG" {
		t.Fatalf("reply0 %+v", got[0])
	}
	if string(got[1].Bulk) != "hello" {
		t.Fatalf("reply1 %+v", got[1])
	}
	if string(got[2].Bulk) != "abc" {
		t.Fatalf("reply2 %+v", got[2])
	}
}

func TestEmptyStringVsNull(t *testing.T) {
	ts, _ := newTestServer(t)
	e := refEncoder{}
	e.cmd("SET", "e", "")
	e.cmd("GET", "e")
	e.cmd("GET", "missing")
	status, body := doRaw(t, ts, e.b.Bytes(), nil)
	if status != http.StatusOK {
		t.Fatalf("status %d", status)
	}
	got := mustDecode(t, body)
	if got[1].Kind != resp.KindBulk || got[1].Bulk == nil || len(got[1].Bulk) != 0 {
		t.Fatalf("empty string: %+v", got[1])
	}
	if !got[2].IsNull() || got[2].Bulk != nil {
		t.Fatalf("missing key must be null bulk: %+v", got[2])
	}
}

func TestBinarySafeRoundTrip(t *testing.T) {
	ts, _ := newTestServer(t)
	// Build with production encoder, decode with test decoder; and verify
	// with the independent encoder's frame layout afterwards.
	payload := []byte{0x00, 0x01, '\r', '\n', 0xFF, 0x7F}
	cmd := resp.ArrayValue(
		resp.BulkString("SET"), resp.BulkString("bin"), resp.BulkBytes(payload))
	var req bytes.Buffer
	if err := resp.NewEncoder(&req).Encode(cmd); err != nil {
		t.Fatal(err)
	}
	// GET frame via independent encoder (string concatenation).
	e := refEncoder{}
	e.cmd("GET", "bin")
	req.Write(e.b.Bytes())

	status, body := doRaw(t, ts, req.Bytes(), nil)
	if status != http.StatusOK {
		t.Fatalf("status %d: %q", status, body)
	}
	got := mustDecode(t, body)
	if len(got) != 2 {
		t.Fatalf("replies: %d", len(got))
	}
	if !bytes.Equal(got[1].Bulk, payload) {
		t.Fatalf("binary payload mismatch: %v", got[1].Bulk)
	}
}

// ---------------------------------------------------------------------------
// send the whole request body ONE BYTE at a time over a real TCP socket,
// then read the full HTTP response.
// ---------------------------------------------------------------------------

func TestRawHTTPOneByteAtATime(t *testing.T) {
	ts, _ := newTestServer(t)

	e := refEncoder{}
	for i := 0; i < 100; i++ {
		e.cmd("INCR", "counter")
	}
	e.cmd("GET", "counter")
	body := e.b.Bytes()

	status, replyBody := rawSendByteByByte(t, ts, body, nil)
	if status != http.StatusOK {
		t.Fatalf("status %d: %q", status, replyBody)
	}
	got := mustDecode(t, replyBody)
	if len(got) != 101 {
		t.Fatalf("want 101 replies, got %d", len(got))
	}
	for i := 0; i < 100; i++ {
		if got[i].N != int64(i+1) {
			t.Fatalf("incr %d = %d", i, got[i].N)
		}
	}
	if string(got[100].Bulk) != "100" {
		t.Fatalf("final GET = %q", got[100].Bulk)
	}
}

// rawSendByteByByte opens a raw TCP connection to the httptest server,
// writes an HTTP/1.1 request whose body is delivered one byte per Write
// with small delays, then parses the complete HTTP response.
func rawSendByteByByte(t *testing.T, ts *httptest.Server, body []byte, hdr map[string]string) (int, []byte) {
	t.Helper()
	addr := strings.TrimPrefix(ts.URL, "http://")
	conn, err := net.DialTimeout("tcp", addr, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	var reqBuf bytes.Buffer
	fmt.Fprintf(&reqBuf, "POST /resp HTTP/1.1\r\nHost: %s\r\n", addr)
	fmt.Fprintf(&reqBuf, "Content-Type: application/octet-stream\r\n")
	fmt.Fprintf(&reqBuf, "Content-Length: %d\r\nConnection: close\r\n", len(body))
	for k, v := range hdr {
		fmt.Fprintf(&reqBuf, "%s: %s\r\n", k, v)
	}
	reqBuf.WriteString("\r\n")
	reqBuf.Write(body)

	// One byte per write; the first bytes are the HTTP request line so this
	// also exercises the net/http server's own tolerance of slow clients.
	raw := reqBuf.Bytes()
	for i := 0; i < len(raw); i++ {
		if _, err := conn.Write(raw[i : i+1]); err != nil {
			t.Fatalf("write byte %d: %v", i, err)
		}
		if i%256 == 0 {
			time.Sleep(time.Millisecond)
		}
	}
	br := bufio.NewReader(conn)
	respHTTP, err := http.ReadResponse(br, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer respHTTP.Body.Close()
	data, _ := io.ReadAll(respHTTP.Body)
	return respHTTP.StatusCode, data
}

// ---------------------------------------------------------------------------
// half packet: client declares Content-Length but disconnects before the
// body finishes. Server must not panic and must answer on a fresh
// connection afterwards.
// ---------------------------------------------------------------------------

func TestHalfPacketDisconnect(t *testing.T) {
	ts, _ := newTestServer(t)
	addr := strings.TrimPrefix(ts.URL, "http://")

	// A command that completed, then a bulk header promising 100 bytes but
	// only 3 arrive — then the TCP connection is torn down.
	e := refEncoder{}
	e.cmd("SET", "k1", "v1")
	e.raw("*3\r\n$3\r\nSET\r\n$2\r\nk2\r\n$100\r\nabc") // truncated bulk
	full := e.b.Bytes()

	conn, err := net.DialTimeout("tcp", addr, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	var reqBuf bytes.Buffer
	fmt.Fprintf(&reqBuf, "POST /resp HTTP/1.1\r\nHost: %s\r\n", addr)
	// Declare a larger Content-Length than we will actually send: the
	// server must see an unexpected EOF while reading the body.
	fmt.Fprintf(&reqBuf, "Content-Length: %d\r\nConnection: close\r\n\r\n",
		len(full)+97)
	reqBuf.Write(full)
	if _, err := conn.Write(reqBuf.Bytes()); err != nil {
		t.Fatal(err)
	}
	// Tear down immediately (half packet + disconnect).
	_ = conn.Close()

	// Give the server a brief moment to reap the dead conn, then verify a
	// completely fresh connection still serves traffic.
	time.Sleep(50 * time.Millisecond)
	e2 := refEncoder{}
	e2.cmd("PING")
	status, body := doRaw(t, ts, e2.b.Bytes(), nil)
	if status != http.StatusOK || !strings.HasPrefix(string(body), "+PONG") {
		t.Fatalf("server unhealthy after half-packet: %d %q", status, body)
	}
}

// TestHalfPacketInRequestBody covers the in-band case where the body itself
// ends mid-frame but the HTTP layer delivers EOF cleanly (Content-Length
// matching the truncated bytes).
func TestHalfPacketInRequestBody(t *testing.T) {
	ts, _ := newTestServer(t)
	e := refEncoder{}
	e.cmd("SET", "k1", "v1")
	e.raw("*3\r\n$3\r\nSET\r\n$2\r\nk2\r\n$100\r\nabc") // truncated
	status, body := doRaw(t, ts, e.b.Bytes(), nil)
	if status != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", status)
	}
	got := mustDecode(t, body)
	if len(got) != 2 {
		t.Fatalf("want [+OK, -ERR Protocol error], got %d replies: %v", len(got), got)
	}
	if got[0].Kind != resp.KindSimple || got[0].Str != "OK" {
		t.Fatalf("first reply should be the SET that completed: %+v", got[0])
	}
	if got[1].Kind != resp.KindError || !strings.Contains(got[1].Str, "Protocol error") {
		t.Fatalf("terminal protocol error expected, got %+v", got[1])
	}
}

// ---------------------------------------------------------------------------
// negative lengths / malformed frames over HTTP
// ---------------------------------------------------------------------------

func TestNegativeLengthOverHTTP(t *testing.T) {
	ts, _ := newTestServer(t)
	for _, bad := range []string{
		"$-2\r\n",
		"*-3\r\n",
		"*1\r\n$-2\r\n",
		"$5\r\nabc\r\n", // declared length mismatch
	} {
		status, body := doRaw(t, ts, []byte(bad), nil)
		if status != http.StatusBadRequest {
			t.Fatalf("%q: want 400 got %d body %q", bad, status, body)
		}
		got := mustDecode(t, body)
		if len(got) != 1 || got[0].Kind != resp.KindError {
			t.Fatalf("%q: want one error reply, got %v", bad, got)
		}
	}
}

// ---------------------------------------------------------------------------
// transactions
// ---------------------------------------------------------------------------

func TestMultiExecHappy(t *testing.T) {
	ts, _ := newTestServer(t)
	e := refEncoder{}
	e.cmd("MULTI")
	e.cmd("SET", "txk", "1")
	e.cmd("INCR", "txk")
	e.cmd("INCR", "txk")
	e.cmd("GET", "txk")
	e.cmd("EXEC")
	status, body := doRaw(t, ts, e.b.Bytes(), nil)
	if status != http.StatusOK {
		t.Fatalf("status %d: %q", status, body)
	}
	got := mustDecode(t, body)
	wantKinds := []resp.Kind{
		resp.KindSimple, // OK (MULTI)
		resp.KindSimple, // QUEUED
		resp.KindSimple, // QUEUED
		resp.KindSimple, // QUEUED
		resp.KindSimple, // QUEUED
		resp.KindArray,  // EXEC result
	}
	if len(got) != len(wantKinds) {
		t.Fatalf("replies %d: %v", len(got), got)
	}
	for i, k := range wantKinds {
		if got[i].Kind != k {
			t.Fatalf("reply %d kind = %v, want %v (%+v)", i, got[i].Kind, k, got[i])
		}
	}
	exec := got[5]
	if len(exec.Array) != 4 {
		t.Fatalf("exec results: %+v", exec)
	}
	if exec.Array[0].Str != "OK" {
		t.Fatalf("queued SET result %+v", exec.Array[0])
	}
	if exec.Array[1].N != 2 || exec.Array[2].N != 3 {
		t.Fatalf("incr results %+v", exec.Array)
	}
	if string(exec.Array[3].Bulk) != "3" {
		t.Fatalf("queued GET %+v", exec.Array[3])
	}
}

func TestMultiExecSyntaxErrorAborts(t *testing.T) {
	ts, _ := newTestServer(t)
	e := refEncoder{}
	e.cmd("MULTI")
	e.cmd("SET", "a", "1")
	e.cmd("BOGUSCOMMAND", "x") // unknown at queue time
	e.cmd("SET", "b", "2")
	e.cmd("EXEC")
	status, body := doRaw(t, ts, e.b.Bytes(), nil)
	if status != http.StatusOK {
		t.Fatalf("status %d", status)
	}
	got := mustDecode(t, body)
	if len(got) != 5 {
		t.Fatalf("replies: %v", got)
	}
	if got[2].Kind != resp.KindError || !strings.Contains(got[2].Str, "unknown command") {
		t.Fatalf("queue-time unknown command: %+v", got[2])
	}
	if got[4].Kind != resp.KindError || !strings.HasPrefix(got[4].Str, "EXECABORT") {
		t.Fatalf("EXEC must abort: %+v", got[4])
	}
	// Nothing ran.
	e2 := refEncoder{}
	e2.cmd("EXISTS", "a", "b")
	status, body = doRaw(t, ts, e2.b.Bytes(), nil)
	if status != http.StatusOK {
		t.Fatalf("status %d", status)
	}
	if mustDecode(t, body)[0].N != 0 {
		t.Fatal("aborted tx must not execute any queued command")
	}

	// EXEC again reports WITHOUT MULTI, session remains usable.
	e3 := refEncoder{}
	e3.cmd("EXEC")
	_, body = doRaw(t, ts, e3.b.Bytes(), nil)
	if !strings.Contains(string(body), "without MULTI") {
		t.Fatalf("post-abort state: %q", body)
	}
}

func TestMultiExecArityErrorAborts(t *testing.T) {
	ts, _ := newTestServer(t)
	e := refEncoder{}
	e.cmd("MULTI")
	e.cmd("GET") // arity 2, only name given
	e.cmd("EXEC")
	_, body := doRaw(t, ts, e.b.Bytes(), nil)
	got := mustDecode(t, body)
	if !strings.Contains(got[1].Str, "wrong number of arguments") {
		t.Fatalf("want arity error, got %+v", got[1])
	}
	if !strings.HasPrefix(got[2].Str, "EXECABORT") {
		t.Fatalf("want EXECABORT, got %+v", got[2])
	}
}

func TestMultiDiscard(t *testing.T) {
	ts, _ := newTestServer(t)
	e := refEncoder{}
	e.cmd("MULTI")
	e.cmd("SET", "x", "y")
	e.cmd("DISCARD")
	e.cmd("GET", "x")
	_, body := doRaw(t, ts, e.b.Bytes(), nil)
	got := mustDecode(t, body)
	if got[2].Str != "OK" {
		t.Fatalf("discard: %+v", got[2])
	}
	if !got[3].IsNull() {
		t.Fatalf("discarded SET must not have run, got %+v", got[3])
	}
}

func TestRuntimeErrorsInsideExecArePerCommand(t *testing.T) {
	ts, _ := newTestServer(t)
	e := refEncoder{}
	e.cmd("SET", "s", "notanint")
	e.cmd("MULTI")
	e.cmd("INCR", "s")           // runtime error
	e.cmd("SET", "after", "yes") // still runs
	e.cmd("EXEC")
	_, body := doRaw(t, ts, e.b.Bytes(), nil)
	got := mustDecode(t, body)
	exec := got[4]
	if exec.Kind != resp.KindArray || len(exec.Array) != 2 {
		t.Fatalf("exec: %+v", exec)
	}
	if exec.Array[0].Kind != resp.KindError {
		t.Fatalf("INCR error: %+v", exec.Array[0])
	}
	if exec.Array[1].Str != "OK" {
		t.Fatalf("SET after runtime error still runs: %+v", exec.Array[1])
	}
	e2 := refEncoder{}
	e2.cmd("GET", "after")
	_, body = doRaw(t, ts, e2.b.Bytes(), nil)
	if string(mustDecode(t, body)[0].Bulk) != "yes" {
		t.Fatalf("SET in partially-errored EXEC did not persist: %q", body)
	}
}

func TestExecWithoutMulti(t *testing.T) {
	ts, _ := newTestServer(t)
	_, body := doRaw(t, ts, mustFrame(t, "EXEC"), nil)
	if !strings.Contains(string(body), "EXEC without MULTI") {
		t.Fatalf("body %q", body)
	}
}

// ---------------------------------------------------------------------------
// sessions across requests
// ---------------------------------------------------------------------------

func TestSessionAcrossRequests(t *testing.T) {
	ts, _ := newTestServer(t)
	hdr := map[string]string{"X-Session": "sess-1"}

	_, body := doRaw(t, ts, mustFrame(t, "MULTI"), hdr)
	if mustDecode(t, body)[0].Str != "OK" {
		t.Fatalf("MULTI %q", body)
	}
	_, body = doRaw(t, ts, mustFrame(t, "SET", "k", "v"), hdr)
	if mustDecode(t, body)[0].Str != "QUEUED" {
		t.Fatalf("QUEUED %q", body)
	}
	_, body = doRaw(t, ts, mustFrame(t, "EXEC"), hdr)
	got := mustDecode(t, body)
	if got[0].Kind != resp.KindArray || got[0].Array[0].Str != "OK" {
		t.Fatalf("cross-request EXEC: %+v", got)
	}

	// Another session is independent.
	hdr2 := map[string]string{"X-Session": "sess-2"}
	_, body = doRaw(t, ts, mustFrame(t, "EXEC"), hdr2)
	if !strings.Contains(string(body), "without MULTI") {
		t.Fatalf("session isolation broken: %q", body)
	}
}

func TestSessionSelectIsolation(t *testing.T) {
	ts, _ := newTestServer(t)
	h := map[string]string{"X-Session": "sel"}
	doRaw(t, ts, mustFrame(t, "SELECT", "3"), h)
	doRaw(t, ts, mustFrame(t, "SET", "k", "in-db-3"), h)

	// Same session, same db.
	_, body := doRaw(t, ts, mustFrame(t, "GET", "k"), h)
	if string(mustDecode(t, body)[0].Bulk) != "in-db-3" {
		t.Fatalf("selected DB lost")
	}
	// Ephemeral session defaults to db 0.
	_, body = doRaw(t, ts, mustFrame(t, "GET", "k"), nil)
	if !mustDecode(t, body)[0].IsNull() {
		t.Fatal("db3 key leaked into db0")
	}
}

func TestInvalidSessionHeader(t *testing.T) {
	ts, _ := newTestServer(t)
	status, _ := doRaw(t, ts, mustFrame(t, "PING"),
		map[string]string{"X-Session": "bad space"})
	if status != http.StatusBadRequest {
		t.Fatalf("want 400 for invalid session id, got %d", status)
	}
}

// ---------------------------------------------------------------------------
// body limits
// ---------------------------------------------------------------------------

func TestBodyTooLarge(t *testing.T) {
	ts, _ := newTestServer(t,
		server.WithMaxBody(64))
	status, _ := doRaw(t, ts, bytes.Repeat([]byte("x"), 65), nil)
	if status != http.StatusRequestEntityTooLarge {
		t.Fatalf("want 413, got %d", status)
	}
}

// ---------------------------------------------------------------------------
// JSON convenience endpoint
// ---------------------------------------------------------------------------

func TestExecJSON(t *testing.T) {
	ts, _ := newTestServer(t)
	post := func(payload string) (int, map[string]any) {
		resp2, err := http.Post(ts.URL+"/exec", "application/json",
			strings.NewReader(payload))
		if err != nil {
			t.Fatal(err)
		}
		defer resp2.Body.Close()
		var m map[string]any
		raw, _ := io.ReadAll(resp2.Body)
		_ = json.Unmarshal(raw, &m)
		return resp2.StatusCode, m
	}
	status, m := post(`{"command":["SET","j","hello"]}`)
	if status != 200 || m["type"] != "simple" || m["value"] != "OK" {
		t.Fatalf("SET json: %d %v", status, m)
	}
	status, m = post(`{"command":["GET","j"]}`)
	if status != 200 || m["type"] != "bulk" || m["value"] != "hello" {
		t.Fatalf("GET json: %d %v", status, m)
	}
	status, m = post(`{"command":["GET","nope"]}`)
	if status != 200 || m["null"] != true {
		t.Fatalf("null json: %d %v", status, m)
	}
	status, _ = post(`{"command":["INCR","j"]}`)
	if status != 200 {
		t.Fatalf("runtime errors stay 200 with error body")
	}
}

func TestJSONTransaction(t *testing.T) {
	ts, _ := newTestServer(t)
	do := func(c []string) map[string]any {
		b, _ := json.Marshal(map[string]any{"session": "jtx", "command": c})
		resp2, err := http.Post(ts.URL+"/exec", "application/json", bytes.NewReader(b))
		if err != nil {
			t.Fatal(err)
		}
		defer resp2.Body.Close()
		var m map[string]any
		raw, _ := io.ReadAll(resp2.Body)
		_ = json.Unmarshal(raw, &m)
		return m
	}
	if do([]string{"MULTI"})["value"] != "OK" {
		t.Fatal("multi")
	}
	if do([]string{"SET", "a", "1"})["value"] != "QUEUED" {
		t.Fatal("queued")
	}
	m := do([]string{"EXEC"})
	if m["type"] != "array" {
		t.Fatalf("exec %v", m)
	}
}

// ---------------------------------------------------------------------------
// independent encoder cross-check at the HTTP level
// ---------------------------------------------------------------------------

func TestIndependentEncoderWireRoundTrip(t *testing.T) {
	ts, _ := newTestServer(t)
	// Build identical pipelines with both encoders; replies must match.
	commands := [][]string{
		{"MSET", "a", "1", "b", "2"},
		{"MGET", "a", "b", "c"},
		{"INCRBY", "a", "41"},
		{"STRLEN", "b"},
		{"APPEND", "b", "xx"},
	}
	var ref bytes.Buffer
	var prod bytes.Buffer
	re := refEncoder{}
	for _, c := range commands {
		re.cmd(c...)
		ref.Write(re.b.Bytes())
		re.b.Reset()
		elems := make([]resp.Value, len(c))
		for i, p := range c {
			elems[i] = resp.BulkString(p)
		}
		if err := resp.NewEncoder(&prod).Encode(resp.ArrayValue(elems...)); err != nil {
			t.Fatal(err)
		}
	}
	if !bytes.Equal(ref.Bytes(), prod.Bytes()) {
		t.Fatalf("encoders disagree:\nref %q\nprd %q", ref.Bytes(), prod.Bytes())
	}
	_, bodyRef := doRaw(t, ts, ref.Bytes(), nil)
	_, bodyProd := doRaw(t, ts, prod.Bytes(), nil)
	if !bytes.Equal(bodyRef, bodyProd) {
		t.Fatalf("replies differ:\nref %q\nprd %q", bodyRef, bodyProd)
	}
	got := mustDecode(t, bodyRef)
	// MSET ok; MGET [1,2,null]; INCRBY 42; STRLEN 1; APPEND 3
	if got[0].Str != "OK" {
		t.Fatalf("mset %+v", got[0])
	}
	if got[1].Array[2].IsNull() != true {
		t.Fatalf("mget null element %+v", got[1].Array[2])
	}
	if got[2].N != 42 {
		t.Fatalf("incrby %d", got[2].N)
	}
	if got[3].N != 1 {
		t.Fatalf("strlen %d", got[3].N)
	}
	if got[4].N != 3 {
		t.Fatalf("append %d", got[4].N)
	}
}

func TestConcurrentSessions(t *testing.T) {
	ts, _ := newTestServer(t)
	var wg sync.WaitGroup
	for g := 0; g < 16; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			hdr := map[string]string{"X-Session": fmt.Sprintf("g%d", g)}
			for i := 0; i < 50; i++ {
				status, _ := doRaw(t, ts, mustFrame(t, "INCR", fmt.Sprintf("g%d", g)), hdr)
				if status != http.StatusOK {
					t.Errorf("group %d status %d", g, status)
					return
				}
			}
		}(g)
	}
	wg.Wait()
}

func TestHealthAndIndex(t *testing.T) {
	ts, _ := newTestServer(t)
	r, err := http.Get(ts.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	if r.StatusCode != 200 {
		t.Fatalf("health %d", r.StatusCode)
	}
	r.Body.Close()

	resp2, err := http.Get(ts.URL + "/nope")
	if err != nil {
		t.Fatal(err)
	}
	if resp2.StatusCode != 404 {
		t.Fatalf("want 404, got %d", resp2.StatusCode)
	}
	resp2.Body.Close()

	resp3, err := http.Get(ts.URL + "/resp")
	if err != nil {
		t.Fatal(err)
	}
	if resp3.StatusCode != 405 {
		t.Fatalf("want 405 for GET /resp, got %d", resp3.StatusCode)
	}
	resp3.Body.Close()
}

func mustFrame(t *testing.T, parts ...string) []byte {
	t.Helper()
	var b bytes.Buffer
	elems := make([]resp.Value, len(parts))
	for i, p := range parts {
		elems[i] = resp.BulkString(p)
	}
	if err := resp.NewEncoder(&b).Encode(resp.ArrayValue(elems...)); err != nil {
		t.Fatal(err)
	}
	return b.Bytes()
}
