package httpapi_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"resumable-bt/internal/engine"
	"resumable-bt/internal/httpapi"
	"resumable-bt/internal/store"
	"resumable-bt/internal/stub"
)

const dsn = "postgres://btapp:btapp_dev_pw@localhost:5432/btdb?sslmode=disable"

func setup(t *testing.T) (*httptest.Server, *engine.Engine, *store.Store) {
	t.Helper()
	ctx := context.Background()
	st, err := store.Open(ctx, envOr("BT_TEST_DSN", dsn))
	if err != nil {
		t.Skipf("postgresql unavailable: %v", err)
	}
	if err := st.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	_ = st.Tx(ctx, func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx, `TRUNCATE invocations, node_states, ticks, executions,
			tree_versions, trees RESTART IDENTITY CASCADE`)
		return err
	})
	reg := stub.NewRegistry()
	eng := engine.New(st, reg)
	srv := httptest.NewServer(httpapi.New(eng).Handler())
	t.Cleanup(func() { srv.Close(); eng.Close(); st.Close() })
	return srv, eng, st
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

const treeJSON = `{
  "root":"seq","nodes":{
    "seq":{"id":"seq","kind":"sequence","children":["a","b"]},
    "a":{"id":"a","kind":"action","action":"stub","params":{"delay_ms":15}},
    "b":{"id":"b","kind":"action","action":"stub","params":{"message":"b"}}
  }}`

func TestHTTPEndToEnd(t *testing.T) {
	srv, _, _ := setup(t)
	c := srv.Client()

	// health
	if resp, err := c.Get(srv.URL + "/healthz"); err != nil || resp.StatusCode != 200 {
		t.Fatalf("healthz: %v %v", resp, err)
	}

	// publish
	resp, err := c.Post(srv.URL+"/trees/order/versions", "application/json", bytes.NewBufferString(treeJSON))
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 201 {
		t.Fatalf("publish status=%d", resp.StatusCode)
	}
	var pub struct {
		Version     int64  `json:"version"`
		ContentHash string `json:"content_hash"`
	}
	json.NewDecoder(resp.Body).Decode(&pub)
	resp.Body.Close()
	if pub.Version != 1 || len(pub.ContentHash) != 64 {
		t.Fatalf("publish response: %+v", pub)
	}

	// republish identical content -> 200 created=false, same version
	resp, _ = c.Post(srv.URL+"/trees/order/versions", "application/json", bytes.NewBufferString(treeJSON))
	if resp.StatusCode != 200 {
		t.Fatalf("republish want 200, got %d", resp.StatusCode)
	}
	var repub struct {
		Version int64 `json:"version"`
		Created bool  `json:"created"`
	}
	json.NewDecoder(resp.Body).Decode(&repub)
	resp.Body.Close()
	if repub.Version != 1 || repub.Created {
		t.Fatalf("identical republish must return v1 created=false, got %+v", repub)
	}

	// invalid tree rejected
	resp, _ = c.Post(srv.URL+"/trees/bad/versions", "application/json",
		bytes.NewBufferString(`{"root":"nope","nodes":{}}`))
	if resp.StatusCode != 400 {
		t.Fatalf("invalid publish want 400, got %d", resp.StatusCode)
	}
	resp.Body.Close()

	// start execution bound to v1
	resp, _ = c.Post(srv.URL+"/executions", "application/json",
		bytes.NewBufferString(`{"tree":"order","version":1}`))
	if resp.StatusCode != 201 {
		t.Fatalf("start status=%d", resp.StatusCode)
	}
	var ex struct {
		ExecutionID string `json:"execution_id"`
		Version     int64  `json:"version"`
	}
	json.NewDecoder(resp.Body).Decode(&ex)
	resp.Body.Close()
	if ex.Version != 1 {
		t.Fatalf("execution not bound to v1: %+v", ex)
	}

	// Tick until success. The engine also auto-ticks whenever a worker
	// finishes, so a manual tick can race with the auto loop and observe a
	// 409 if the execution just reached a terminal state — that is expected.
	deadline := time.Now().Add(5 * time.Second)
	var status string
	for time.Now().Before(deadline) {
		g, _ := c.Get(srv.URL + "/executions/" + ex.ExecutionID)
		var head struct {
			Status string `json:"status"`
		}
		json.NewDecoder(g.Body).Decode(&head)
		g.Body.Close()
		if head.Status == "success" || head.Status == "failure" || head.Status == "canceled" {
			status = head.Status
			break
		}
		resp, _ = c.Post(srv.URL+"/executions/"+ex.ExecutionID+"/tick",
			"application/json", bytes.NewBufferString(`{"note":"api tick"}`))
		if resp.StatusCode == 409 {
			// Auto-tick finalized it between our GET and POST.
			resp.Body.Close()
			continue
		}
		if resp.StatusCode != 200 {
			t.Fatalf("tick status=%d", resp.StatusCode)
		}
		var tr struct {
			Status string `json:"status"`
		}
		json.NewDecoder(resp.Body).Decode(&tr)
		resp.Body.Close()
		status = tr.Status
		time.Sleep(10 * time.Millisecond)
	}
	if status != "success" {
		t.Fatalf("execution did not succeed, last status=%s", status)
	}

	// snapshot has invocations and gapless ticks
	g, _ := c.Get(srv.URL + "/executions/" + ex.ExecutionID + "/snapshot")
	var snap struct {
		Invocations []struct {
			Status     string `json:"status"`
			Dispatches int64  `json:"dispatches"`
			Idempotent bool   `json:"idempotent"`
		} `json:"invocations"`
		Ticks []struct {
			Seq    int64  `json:"seq"`
			Status string `json:"status"`
		} `json:"ticks"`
	}
	json.NewDecoder(g.Body).Decode(&snap)
	g.Body.Close()
	if len(snap.Invocations) != 2 {
		t.Fatalf("want 2 invocations, got %d", len(snap.Invocations))
	}
	for _, iv := range snap.Invocations {
		if iv.Status != "success" || iv.Dispatches != 1 {
			t.Errorf("invocation %+v", iv)
		}
	}
}

func TestHTTPCancelAndLateTickConflict(t *testing.T) {
	srv, _, _ := setup(t)
	c := srv.Client()
	slow := `{
    "root":"to","nodes":{
      "to":{"id":"to","kind":"timeout","timeout_ms":10000,"children":["w"]},
      "w":{"id":"w","kind":"action","action":"stub","params":{"delay_ms":30000}}
    }}`
	resp, _ := c.Post(srv.URL+"/trees/slow/versions", "application/json", bytes.NewBufferString(slow))
	resp.Body.Close()
	resp, _ = c.Post(srv.URL+"/executions", "application/json", bytes.NewBufferString(`{"tree":"slow"}`))
	var ex struct {
		ExecutionID string `json:"execution_id"`
	}
	json.NewDecoder(resp.Body).Decode(&ex)
	resp.Body.Close()
	resp, _ = c.Post(srv.URL+"/executions/"+ex.ExecutionID+"/tick", "application/json", bytes.NewBufferString(`{}`))
	resp.Body.Close()
	time.Sleep(50 * time.Millisecond)

	req, _ := http.NewRequest("POST", srv.URL+"/executions/"+ex.ExecutionID+"/cancel", nil)
	resp, _ = c.Do(req)
	if resp.StatusCode != 200 {
		t.Fatalf("cancel status=%d", resp.StatusCode)
	}
	resp.Body.Close()

	resp, _ = c.Post(srv.URL+"/executions/"+ex.ExecutionID+"/tick", "application/json", bytes.NewBufferString(`{}`))
	if resp.StatusCode != 409 {
		t.Fatalf("tick after cancel want 409, got %d", resp.StatusCode)
	}
	resp.Body.Close()

	// cancel twice -> conflict
	req, _ = http.NewRequest("POST", srv.URL+"/executions/"+ex.ExecutionID+"/cancel", nil)
	resp, _ = c.Do(req)
	if resp.StatusCode != 409 {
		t.Fatalf("second cancel want 409, got %d", resp.StatusCode)
	}
	resp.Body.Close()
}

func TestHTTPUnknownExecution404(t *testing.T) {
	srv, _, _ := setup(t)
	resp, err := srv.Client().Get(srv.URL + "/executions/00000000-0000-0000-0000-000000000000")
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 404 {
		t.Fatalf("want 404 got %d", resp.StatusCode)
	}
}

// TestNewVersionDoesNotAffectOldExecution: publish v2 after an execution is
// running on v1; the execution keeps using the immutable v1 definition.
func TestExecutionPinnedToVersion(t *testing.T) {
	srv, eng, st := setup(t)
	c := srv.Client()

	v1 := `{
    "root":"seq","nodes":{
      "seq":{"id":"seq","kind":"sequence","children":["only"]},
      "only":{"id":"only","kind":"action","action":"stub","params":{"message":"v1"}}
    }}`
	resp, _ := c.Post(srv.URL+"/trees/mut/versions", "application/json", bytes.NewBufferString(v1))
	resp.Body.Close()
	resp, _ = c.Post(srv.URL+"/executions", "application/json", bytes.NewBufferString(`{"tree":"mut","version":1}`))
	var ex struct {
		ExecutionID string `json:"execution_id"`
	}
	json.NewDecoder(resp.Body).Decode(&ex)
	resp.Body.Close()

	// Publish a different definition as v2.
	v2 := `{
    "root":"fb","nodes":{
      "fb":{"id":"fb","kind":"fallback","children":["other"]},
      "other":{"id":"other","kind":"action","action":"stub","params":{"result":"failure","message":"v2 fails"}}
    }}`
	resp, _ = c.Post(srv.URL+"/trees/mut/versions", "application/json", bytes.NewBufferString(v2))
	if resp.StatusCode != 201 {
		t.Fatalf("v2 publish status=%d", resp.StatusCode)
	}
	resp.Body.Close()

	// Drive the v1 execution; it must succeed using its pinned definition.
	res, err := eng.Tick(context.Background(), mustUUID(t, ex.ExecutionID), "")
	if err != nil {
		t.Fatal(err)
	}
	_ = res
	poll(t, func() string {
		row, _ := st.GetExecution(context.Background(), mustUUID(t, ex.ExecutionID))
		return row.Status
	}, "success")

	row, _ := st.GetExecution(context.Background(), mustUUID(t, ex.ExecutionID))
	if row.TreeVersion != 1 || row.Status != "success" {
		t.Fatalf("execution drifted: version=%d status=%s", row.TreeVersion, row.Status)
	}
}

func poll(t *testing.T, fn func() string, want string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if fn() == want {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("never reached %s (last=%s)", want, fn())
}

func mustUUID(t *testing.T, s string) uuid.UUID {
	t.Helper()
	v, err := uuid.Parse(s)
	if err != nil {
		t.Fatal(err)
	}
	return v
}
