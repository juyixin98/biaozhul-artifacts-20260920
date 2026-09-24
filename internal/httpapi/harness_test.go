package httpapi_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"shardmigrator/internal/cluster"
	"shardmigrator/internal/httpapi"
)

// ---- test harness ---------------------------------------------------------

type testServer struct {
	t    *testing.T
	cl   *cluster.Cluster
	http *httptest.Server
}

func newTestServer(t *testing.T) *testServer {
	t.Helper()
	cl := cluster.NewCluster()
	if err := cl.AddNode("node-a"); err != nil {
		t.Fatal(err)
	}
	if err := cl.AddNode("node-b"); err != nil {
		t.Fatal(err)
	}
	if err := cl.CreateShard("orders", []string{"node-a", "node-b"}); err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(httpapi.NewServer(cl).Handler())
	t.Cleanup(ts.Close)
	return &testServer{t: t, cl: cl, http: ts}
}

type apiResp struct {
	status int
	body   map[string]any
	raw    []byte
}

func (r apiResp) requireOK() {
	if r.status/100 != 2 {
		panic(apiErr(fmt.Sprintf("expected 2xx, got %d: %s", r.status, string(r.raw))))
	}
}

type apiErr string

func (e apiErr) Error() string { return string(e) }

func (ts *testServer) call(method, path string, body any, headers map[string]string) (resp apiResp) {
	ts.t.Helper()
	defer func() {
		if rec := recover(); rec != nil {
			if ae, ok := rec.(apiErr); ok {
				ts.t.Fatal(string(ae))
			}
			panic(rec)
		}
	}()
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			ts.t.Fatalf("marshal: %v", err)
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, ts.http.URL+path, rdr)
	if err != nil {
		ts.t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	r, err := http.DefaultClient.Do(req)
	if err != nil {
		ts.t.Fatalf("HTTP %s %s: %v", method, path, err)
	}
	defer r.Body.Close()
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		ts.t.Fatal(err)
	}
	resp = apiResp{status: r.StatusCode, raw: raw}
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &resp.body); err != nil {
			ts.t.Fatalf("non-json response (%d): %s", r.StatusCode, raw)
		}
	}
	return resp
}

func (r apiResp) dataMap() map[string]any {
	r.requireOK()
	d, _ := r.body["data"].(map[string]any)
	return d
}

func (r apiResp) replay() bool {
	b, _ := r.body["idempotent_replay"].(bool)
	return b
}

// writeReqBody mirrors the server's POST /writes body.
type writeReqBody struct {
	Payload        string `json:"payload"`
	ClientRouteVer int    `json:"client_route_ver,omitempty"`
	IdempotencyKey string `json:"idempotency_key,omitempty"`
}

func writeAt(ts *testServer, route int, payload, idem string) apiResp {
	body := writeReqBody{Payload: payload, ClientRouteVer: route}
	var hdr map[string]string
	if idem != "" {
		hdr = map[string]string{"Idempotency-Key": idem}
	}
	return ts.call("POST", "/shards/orders/writes", body, hdr)
}

func mustWrite(ts *testServer, route int, payload string) map[string]any {
	r := writeAt(ts, route, payload, "")
	r.requireOK()
	return r.dataMap()
}

func control(ts *testServer, action string, body map[string]any) apiResp {
	if body == nil {
		body = map[string]any{}
	}
	return ts.call("POST", "/shards/orders/migration/"+action, body, nil)
}

func controlKeyed(ts *testServer, action, key string) apiResp {
	return ts.call("POST", "/shards/orders/migration/"+action,
		map[string]string{"idempotency_key": key}, nil)
}

func auditShard(ts *testServer) map[string]any {
	r := ts.call("GET", "/shards/orders/audit", nil, nil)
	r.requireOK()
	return r.dataMap()
}

func asInt(m map[string]any, key string) int {
	v, ok := m[key].(float64)
	if !ok {
		return 0
	}
	return int(v)
}
