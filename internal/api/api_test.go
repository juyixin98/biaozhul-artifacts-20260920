// Package api_test exercises the full HTTP surface against a real database
// and a real TCP listener, including client-side request cancellation to
// interrupt a parked tick.
package api_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"bt/internal/api"
	"bt/internal/engine"
	"bt/internal/model"
	"bt/internal/store"
	"bt/internal/stub"
)

const testDBURL = "postgres://bt065b:bt065b@localhost:5432/btree065b?sslmode=disable"

type env struct {
	baseURL string
	eng     *engine.Engine
	st      *store.Store
	rg      *stub.Registry
	pool    *pgxpool.Pool
	client  *http.Client
}

func newEnv(t *testing.T) *env {
	t.Helper()
	base := os.Getenv("BT_TEST_DATABASE_URL")
	if base == "" {
		base = testDBURL
	}
	admin, err := pgxpool.New(context.Background(), base)
	if err != nil {
		t.Skipf("db unavailable: %v", err)
	}
	if err := admin.Ping(context.Background()); err != nil {
		admin.Close()
		t.Skipf("db unavailable: %v", err)
	}
	schema := "api_" + strings.Map(func(r rune) rune {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '_' {
			return r
		}
		return '_'
	}, t.Name())
	_, _ = admin.Exec(context.Background(), "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
	if _, err := admin.Exec(context.Background(), "CREATE SCHEMA "+schema); err != nil {
		admin.Close()
		t.Fatalf("schema: %v", err)
	}
	admin.Close()

	u, _ := url.Parse(base)
	q := u.Query()
	q.Set("search_path", schema)
	u.RawQuery = q.Encode()
	pool, err := pgxpool.New(context.Background(), u.String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		pool.Close()
		c, _ := pgxpool.New(context.Background(), base)
		if c != nil {
			_, _ = c.Exec(context.Background(), "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
			c.Close()
		}
	})

	st := store.New(pool)
	if err := st.Migrate(context.Background()); err != nil {
		t.Fatal(err)
	}
	rg := stub.NewRegistry()
	eng := engine.New(st, rg, engine.Options{})
	if err := eng.ReapOrphans(context.Background()); err != nil {
		t.Fatal(err)
	}

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{Handler: api.NewServer(eng)}
	go srv.Serve(ln)
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		srv.Shutdown(ctx)
	})

	return &env{
		baseURL: "http://" + ln.Addr().String(),
		eng:     eng, st: st, rg: rg, pool: pool,
		client: &http.Client{Timeout: 30 * time.Second},
	}
}

func (e *env) do(t *testing.T, method, path string, body any) (int, map[string]any) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, e.baseURL+path, rdr)
	if err != nil {
		t.Fatal(err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := e.client.Do(req)
	if err != nil {
		t.Fatalf("%s %s: %v", method, path, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out map[string]any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	return resp.StatusCode, out
}

// Full lifecycle over HTTP: publish -> start -> tick running -> resolve gate
// -> tick success.
func TestHTTPFullLifecycle(t *testing.T) {
	e := newEnv(t)
	tree := map[string]any{"tree": map[string]any{
		"name": "http-demo",
		"root": map[string]any{
			"id": "r", "kind": "root",
			"children": []any{map[string]any{
				"id": "g", "kind": "action", "stub": "gate",
				"args": map[string]any{"token": "T1"},
			}},
		},
	}}
	code, pub := e.do(t, "POST", "/v1/trees", tree)
	if code != http.StatusCreated {
		t.Fatalf("publish code=%d body=%v", code, pub)
	}
	treeID := pub["tree_id"].(string)

	code, start := e.do(t, "POST", "/v1/executions",
		map[string]any{"tree_id": treeID})
	if code != http.StatusCreated {
		t.Fatalf("start code=%d body=%v", code, start)
	}
	execID := start["execution_id"].(string)

	code, tk := e.do(t, "POST", "/v1/executions/"+execID+"/ticks", nil)
	if code != http.StatusOK || tk["tree_status"] != "running" {
		t.Fatalf("tick1 code=%d body=%v", code, tk)
	}
	if seq := tk["seq"].(float64); seq != 1 {
		t.Fatalf("first seq=%v", seq)
	}

	code, _ = e.do(t, "POST", "/internal/gates/T1/resolve",
		map[string]any{"status": "success"})
	if code != http.StatusOK {
		t.Fatalf("resolve code=%d", code)
	}
	// Poll until the watcher persists.
	time.Sleep(100 * time.Millisecond)

	code, tk = e.do(t, "POST", "/v1/executions/"+execID+"/ticks", nil)
	if code != http.StatusOK || tk["tree_status"] != "success" {
		t.Fatalf("tick2 code=%d body=%v", code, tk)
	}
	if seq := tk["seq"].(float64); seq != 2 {
		t.Fatalf("second seq=%v", seq)
	}

	// Terminal execution rejects further ticks with 409.
	code, _ = e.do(t, "POST", "/v1/executions/"+execID+"/ticks", nil)
	if code != http.StatusConflict {
		t.Fatalf("tick after terminal code=%d, want 409", code)
	}
}

// Client disconnecting while the tick is parked interrupts the tick over a
// real HTTP connection: durable state records one interrupted tick at seq 1.
func TestHTTPClientCancelInterruptsTick(t *testing.T) {
	e := newEnv(t)
	tree := map[string]any{"tree": map[string]any{
		"name": "http-interrupt",
		"root": map[string]any{"id": "r", "kind": "root", "children": []any{
			map[string]any{"id": "parked", "kind": "action", "stub": "block",
				"args": map[string]any{"release_ms": 60000}},
		}},
	}}
	_, pub := e.do(t, "POST", "/v1/trees", tree)
	_, start := e.do(t, "POST", "/v1/executions",
		map[string]any{"tree_id": pub["tree_id"].(string)})
	execID := start["execution_id"].(string)

	ctx, cancel := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, "POST",
		e.baseURL+"/v1/executions/"+execID+"/ticks", nil)
	errCh := make(chan error, 1)
	go func() {
		resp, err := e.client.Do(req)
		if resp != nil {
			resp.Body.Close()
		}
		errCh <- err
	}()

	// Let the server park inside the block stub, then disconnect.
	time.Sleep(250 * time.Millisecond)
	cancel()
	select {
	case err := <-errCh:
		if err == nil {
			t.Fatal("expected client-side cancellation error")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("canceled request did not return")
	}

	// Durable state must record one interrupted tick consuming seq 1.
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		code, snap := e.do(t, "GET", "/v1/executions/"+execID, nil)
		if code == http.StatusOK {
			if lt, ok := snap["last_tick"].(map[string]any); ok &&
				lt["status"] == "interrupted" && lt["seq"].(float64) == 1 {
				return
			}
		}
		time.Sleep(20 * time.Millisecond)
	}
	_, snap := e.do(t, "GET", "/v1/executions/"+execID, nil)
	t.Fatalf("interrupted tick not recorded: %v", fmt.Sprintf("%v", snap))
}

// Publishing invalid JSON / unknown fields yields 400.
func TestHTTPRejectsBadInput(t *testing.T) {
	e := newEnv(t)
	resp, err := http.Post(e.baseURL+"/v1/trees", "application/json",
		strings.NewReader(`{"tree":{"name":"x","root":{"id":"r","kind":"root","children":[{"id":"a","kind":"action","stub":"succeed"}],"bogus":1}}}`))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", resp.StatusCode)
	}
}

// Late gate resolution after the gate is gone returns 404 and changes nothing.
func TestHTTPLateGateRejected(t *testing.T) {
	e := newEnv(t)
	tree := map[string]any{"tree": map[string]any{
		"name": "late-gate",
		"root": map[string]any{"id": "r", "kind": "root", "children": []any{
			map[string]any{"id": "g", "kind": "action", "stub": "gate",
				"args": map[string]any{"token": "LG"}},
		}},
	}}
	_, pub := e.do(t, "POST", "/v1/trees", tree)
	_, start := e.do(t, "POST", "/v1/executions",
		map[string]any{"tree_id": pub["tree_id"].(string)})
	execID := start["execution_id"].(string)
	e.do(t, "POST", "/v1/executions/"+execID+"/ticks", nil)
	e.do(t, "POST", "/v1/executions/"+execID+"/abort", nil)
	code, body := e.do(t, "POST", "/internal/gates/LG/resolve",
		map[string]any{"status": "success"})
	if code != http.StatusNotFound {
		t.Fatalf("late resolution code=%d body=%v, want 404", code, body)
	}
}

var _ = model.KindAction
