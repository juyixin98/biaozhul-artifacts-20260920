package server

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"respd/internal/kv"
	"respd/internal/resp"
)

// ---------------------------------------------------------------------------
// Independent request encoder (test-only, does not use resp.Writer)
// ---------------------------------------------------------------------------

func refCommand(args ...string) []byte {
	var b bytes.Buffer
	fmt.Fprintf(&b, "*%d\r\n", len(args))
	for _, a := range args {
		fmt.Fprintf(&b, "$%d\r\n", len(a))
		b.WriteString(a)
		b.WriteString("\r\n")
	}
	return b.Bytes()
}

// oneByteReader feeds the HTTP handler exactly one byte per Read, proving the
// server path does not rely on coalesced reads.
type oneByteReader struct{ data []byte }

func (r *oneByteReader) Read(p []byte) (int, error) {
	if len(r.data) == 0 {
		return 0, io.EOF
	}
	p[0] = r.data[0]
	r.data = r.data[1:]
	return 1, nil
}

func newTestServer(t *testing.T, maxBody int64) (*httptest.Server, *kv.Store) {
	t.Helper()
	store := kv.NewStore()
	srv := httptest.NewServer(New(Config{Store: store, MaxBodyBytes: maxBody}))
	t.Cleanup(srv.Close)
	return srv, store
}

func post(t *testing.T, url string, body []byte) (int, []byte) {
	t.Helper()
	resp2, err := http.Post(url, "application/octet-stream", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	data, err := io.ReadAll(resp2.Body)
	if err != nil {
		t.Fatal(err)
	}
	return resp2.StatusCode, data
}

// readAll parses a pipelined reply stream.
func readAll(t *testing.T, data []byte) []*resp.Value {
	t.Helper()
	r := resp.NewReader(bytes.NewReader(data))
	var out []*resp.Value
	for {
		v, err := r.ReadMessage()
		if errors.Is(err, io.EOF) {
			return out
		}
		if err != nil {
			t.Fatalf("parse reply %q: %v", data, err)
		}
		out = append(out, v)
	}
}

func TestHealthAndCommands(t *testing.T) {
	srv, _ := newTestServer(t, 0)
	r, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(r.Body)
	r.Body.Close()
	if r.StatusCode != 200 || strings.TrimSpace(string(body)) != "ok" {
		t.Fatalf("healthz: %d %q", r.StatusCode, body)
	}
	r2, err := http.Get(srv.URL + "/commands")
	if err != nil {
		t.Fatal(err)
	}
	defer r2.Body.Close()
	if ct := r2.Header.Get("Content-Type"); !strings.Contains(ct, "json") {
		t.Fatalf("commands content-type: %s", ct)
	}
}

func TestMethodNotAllowed(t *testing.T) {
	srv, _ := newTestServer(t, 0)
	r, err := http.Get(srv.URL + "/resp")
	if err != nil {
		t.Fatal(err)
	}
	defer r.Body.Close()
	if r.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("GET /resp: %d", r.StatusCode)
	}
}

func TestEmptyBody(t *testing.T) {
	srv, _ := newTestServer(t, 0)
	status, data := post(t, srv.URL+"/resp", nil)
	if status != http.StatusNoContent || len(data) != 0 {
		t.Fatalf("empty body: %d %q", status, data)
	}
}

func TestPipelineAndBinarySafety(t *testing.T) {
	srv, _ := newTestServer(t, 0)
	// Mixed command set in ONE request, encoded by the independent encoder.
	body := bytes.Join([][]byte{
		refCommand("PING"),
		refCommand("SET", "bin", "a\r\n\x00\xff"),
		refCommand("GET", "bin"),
		refCommand("GET", "missing"),
		refCommand("SET", "empty", ""),
		refCommand("GET", "empty"),
	}, nil)

	status, raw := post(t, srv.URL+"/resp", body)
	if status != 200 {
		t.Fatalf("status %d: %q", status, raw)
	}
	got := readAll(t, raw)
	if len(got) != 6 {
		t.Fatalf("want 6 pipelined replies, got %d: %q", len(got), raw)
	}
	if got[0].Type != resp.SimpleString || got[0].Text != "PONG" {
		t.Fatalf("reply 0: %+v", got[0])
	}
	if got[1].Type != resp.SimpleString || got[1].Text != "OK" {
		t.Fatalf("reply 1: %+v", got[1])
	}
	if string(got[2].Str) != "a\r\n\x00\xff" {
		t.Fatalf("binary round trip: %q", got[2].Str)
	}
	if got[3].Type != resp.NullBulk {
		t.Fatalf("missing key must be null bulk, got %+v", got[3])
	}
	if got[4].Text != "OK" {
		t.Fatalf("SET empty: %+v", got[4])
	}
	if got[5].Type != resp.BulkString || len(got[5].Str) != 0 {
		t.Fatalf("empty string must be bulk of length 0, got %+v", got[5])
	}
}

// Feed the handler one byte at a time (deterministic, no TCP coalescing).
func TestHandlerSingleByteBody(t *testing.T) {
	store := kv.NewStore()
	h := New(Config{Store: store})
	body := bytes.Join([][]byte{
		refCommand("SET", "k", "v"),
		refCommand("GET", "k"),
	}, nil)
	req := httptest.NewRequest(http.MethodPost, "/resp", &oneByteReader{data: body})
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatalf("status %d: %q", rec.Code, rec.Body.Bytes())
	}
	got := readAll(t, rec.Body.Bytes())
	if len(got) != 2 || got[1].Type != resp.BulkString || string(got[1].Str) != "v" {
		t.Fatalf("single-byte pipeline replies: %+v", got)
	}
}

func TestNegativeBulkLength(t *testing.T) {
	srv, _ := newTestServer(t, 0)
	// Valid command followed by a frame with illegal length -2.
	body := append(refCommand("PING"), []byte("$-2\r\n")...)
	status, raw := post(t, srv.URL+"/resp", body)
	if status != 200 {
		t.Fatalf("status: %d", status)
	}
	got := readAll(t, raw)
	if len(got) != 2 || got[0].Text != "PONG" {
		t.Fatalf("pipelined PING should survive: %q", raw)
	}
	if got[1].Type != resp.Error || !strings.Contains(got[1].Text, "-2") {
		t.Fatalf("want error naming -2, got %+v", got[1])
	}
}

func TestHalfPacketDisconnect(t *testing.T) {
	srv, _ := newTestServer(t, 0)
	// Complete SET, then a bulk whose payload never arrives.
	body := append(refCommand("SET", "k", "v1"), []byte("$5\r\nabc")...)
	status, raw := post(t, srv.URL+"/resp", body)
	if status != 200 {
		t.Fatalf("status %d", status)
	}
	got := readAll(t, raw)
	if len(got) != 2 {
		t.Fatalf("want 2 replies (ok, error), got %d: %q", len(got), raw)
	}
	if got[0].Text != "OK" {
		t.Fatalf("earlier command should have replied OK")
	}
	if got[1].Type != resp.Error || !strings.Contains(strings.ToLower(got[1].Text), "eof") {
		t.Fatalf("want unexpected-EOF error, got %+v", got[1])
	}
}

func TestTransactionSyntaxErrorOverHTTP(t *testing.T) {
	srv, _ := newTestServer(t, 0)
	body := bytes.Join([][]byte{
		refCommand("MULTI"),
		refCommand("SET", "k", "1"),
		refCommand("BOGUS", "x"), // unknown command: queue-time error
		refCommand("GET"),        // arity error: queue-time error
		refCommand("EXEC"),       // must EXECABORT
		refCommand("GET", "k"),   // nothing ran: null
	}, nil)
	status, raw := post(t, srv.URL+"/resp", body)
	if status != 200 {
		t.Fatalf("status %d: %q", status, raw)
	}
	got := readAll(t, raw)
	wantKinds := []resp.Type{
		resp.SimpleString, // MULTI -> OK
		resp.SimpleString, // QUEUED
		resp.Error,        // unknown
		resp.Error,        // arity
		resp.Error,        // EXECABORT
		resp.NullBulk,     // k never set
	}
	if len(got) != len(wantKinds) {
		t.Fatalf("want %d replies, got %d: %q", len(wantKinds), len(got), raw)
	}
	for i, want := range wantKinds {
		if got[i].Type != want {
			t.Fatalf("reply %d: want type %d, got %+v (%q)", i, want, got[i], raw)
		}
	}
	if !strings.HasPrefix(got[4].Text, "EXECABORT") {
		t.Fatalf("reply 4: %q", got[4].Text)
	}
}

func TestValidTransactionOverHTTP(t *testing.T) {
	srv, _ := newTestServer(t, 0)
	body := bytes.Join([][]byte{
		refCommand("MULTI"),
		refCommand("INCR", "n"),
		refCommand("INCR", "n"),
		refCommand("EXEC"),
	}, nil)
	_, raw := post(t, srv.URL+"/resp", body)
	got := readAll(t, raw)
	if got[len(got)-1].Type != resp.Array || len(got[3].Array) != 2 {
		t.Fatalf("EXEC reply: %+v", got)
	}
	if got[3].Array[0].Int != 1 || got[3].Array[1].Int != 2 {
		t.Fatalf("tx results: %+v", got[3].Array)
	}
}

func TestOversizedBody(t *testing.T) {
	srv, _ := newTestServer(t, 64) // 64-byte cap for the test
	status, raw := post(t, srv.URL+"/resp", refCommand("PING", strings.Repeat("x", 200)))
	if status != http.StatusRequestEntityTooLarge {
		t.Fatalf("want 413, got %d: %q", status, raw)
	}
}

func TestNonArrayFrameDoesNotKillPipeline(t *testing.T) {
	srv, _ := newTestServer(t, 0)
	body := append([]byte("+PING\r\n"), refCommand("PING")...)
	status, raw := post(t, srv.URL+"/resp", body)
	if status != 200 {
		t.Fatalf("status %d", status)
	}
	got := readAll(t, raw)
	if len(got) != 2 || got[0].Type != resp.Error || got[1].Text != "PONG" {
		t.Fatalf("want error then PONG, got %+v (%q)", got, raw)
	}
}
