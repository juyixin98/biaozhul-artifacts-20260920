package api_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"forkindexer/internal/api"
	"forkindexer/internal/indexer"
	"forkindexer/internal/model"
	"forkindexer/internal/store"

	"github.com/jackc/pgx/v5/pgxpool"
)

var schemaCounter int64

func testServer(t *testing.T) (*httptest.Server, func(byte) string) {
	t.Helper()
	dsn := "host=/var/run/postgresql user=admin dbname=forkindexer_test"
	schema := fmt.Sprintf("http_%d_%d", time.Now().UnixNano(), atomic.AddInt64(&schemaCounter, 1))
	admin, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Skipf("test database unreachable: %v", err)
	}
	if _, err := admin.Exec(context.Background(), "CREATE SCHEMA "+schema); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_, _ = admin.Exec(context.Background(), "DROP SCHEMA "+schema+" CASCADE")
		admin.Close()
	})
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal(err)
	}
	cfg.ConnConfig.RuntimeParams["search_path"] = schema
	pool, err := pgxpool.NewWithConfig(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if err := store.Migrate(context.Background(), pool); err != nil {
		t.Fatal(err)
	}

	ix := indexer.New(pool)
	srv := httptest.NewServer(api.NewServer(ix).Router())
	t.Cleanup(srv.Close)

	addr := func(name byte) string {
		out := make([]byte, 40)
		for i := range out {
			out[i] = '0'
		}
		out[39] = name
		return "0x" + string(out)
	}
	return srv, addr
}

func must(t *testing.T, res *http.Response, want int, into any) []byte {
	t.Helper()
	defer res.Body.Close()
	body := new(bytes.Buffer)
	_, _ = body.ReadFrom(res.Body)
	if res.StatusCode != want {
		t.Fatalf("HTTP %d, want %d: %s", res.StatusCode, want, body.String())
	}
	if into != nil {
		if err := json.Unmarshal(body.Bytes(), into); err != nil {
			t.Fatalf("json: %v body=%s", err, body.String())
		}
	}
	return body.Bytes()
}

func TestIngestAndQueryHappyPath(t *testing.T) {
	srv, addr := testServer(t)
	a, b := addr('a'), addr('b')

	g, _, err := model.Normalize(model.Block{
		Height:       0,
		Transactions: []model.Transfer{{From: "", To: a, Amount: "1000"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	n1, _, err := model.Normalize(model.Block{
		ParentHash: g.Hash, Height: 1,
		Transactions: []model.Transfer{{From: a, To: b, Amount: "250"}},
	})
	if err != nil {
		t.Fatal(err)
	}

	post := func(path string, v any) (*http.Response, error) {
		raw, _ := json.Marshal(v)
		return http.Post(srv.URL+path, "application/json", bytes.NewReader(raw))
	}

	var ing map[string]any
	res, err := post("/v1/blocks", []model.Block{g, n1})
	if err != nil {
		t.Fatal(err)
	}
	must(t, res, 200, &ing)
	if ing["headHash"] != n1.Hash {
		t.Fatalf("headHash = %v", ing["headHash"])
	}

	// head
	var head map[string]any
	must(t, req(t, http.MethodGet, srv.URL+"/v1/head", nil), 200, &head)
	if head["hash"] != n1.Hash || head["height"].(float64) != 1 {
		t.Fatalf("head = %v", head)
	}

	// block by hash
	var blk map[string]any
	must(t, req(t, http.MethodGet, srv.URL+"/v1/blocks/"+n1.Hash, nil), 200, &blk)
	if blk["canonical"] != true || blk["status"] != "connected" {
		t.Fatalf("block = %v", blk)
	}

	// canonical block at height 1
	var atH map[string]any
	must(t, req(t, http.MethodGet, srv.URL+"/v1/height/1", nil), 200, &atH)
	if atH["hash"] != n1.Hash {
		t.Fatalf("height/1 = %v", atH)
	}

	// chain depth
	var chain map[string]any
	must(t, req(t, http.MethodGet, srv.URL+"/v1/chain", nil), 200, &chain)
	if chain["depth"].(float64) != 2 {
		t.Fatalf("chain = %v", chain)
	}

	// balances
	for want := range map[string]string{a: "750", b: "250"} {
		var bal map[string]string
		must(t, req(t, http.MethodGet, srv.URL+"/v1/accounts/"+want+"/balance", nil), 200, &bal)
		exp := map[string]string{a: "750", b: "250"}[want]
		if bal["balance"] != exp {
			t.Fatalf("balance %s = %s want %s", want, bal["balance"], exp)
		}
	}
	var acct map[string]any
	must(t, req(t, http.MethodGet, srv.URL+"/v1/accounts/"+b+"/transactions", nil), 200, &acct)
	if n := len(acct["transactions"].([]any)); n != 1 {
		t.Fatalf("b tx count = %d", n)
	}

	// state cursor
	var state map[string]any
	must(t, req(t, http.MethodGet, srv.URL+"/v1/state", nil), 200, &state)
	if state["ingestSeq"].(float64) != 2 {
		t.Fatalf("state = %v", state)
	}
}

func TestOrphanStagingThenConnectViaNDJSON(t *testing.T) {
	srv, addr := testServer(t)
	a := addr('a')

	g, _, _ := model.Normalize(model.Block{Height: 0})
	child, _, _ := model.Normalize(model.Block{ParentHash: g.Hash, Height: 1,
		Transactions: []model.Transfer{{From: "", To: a, Amount: "9"}}})

	// Child first: staged, head absent.
	res, err := http.Post(srv.URL+"/v1/blocks", "application/x-ndjson",
		bytes.NewReader(mustNDJSON(t, child)))
	if err != nil {
		t.Fatal(err)
	}
	var ing map[string]any
	must(t, res, 200, &ing)
	if ing["blocks"].([]any)[0].(map[string]any)["status"] != "staged" {
		t.Fatalf("child not staged: %v", ing)
	}

	var head map[string]any
	must(t, req(t, http.MethodGet, srv.URL+"/v1/head", nil), 200, &head)
	if head["canonical"] != false {
		t.Fatalf("staged-only head canonical = %v", head)
	}

	// Genesis arrives: child connects, head becomes child.
	res, err = http.Post(srv.URL+"/v1/blocks", "application/x-ndjson",
		bytes.NewReader(mustNDJSON(t, g)))
	if err != nil {
		t.Fatal(err)
	}
	must(t, res, 200, nil)
	must(t, req(t, http.MethodGet, srv.URL+"/v1/head", nil), 200, &head)
	if head["hash"] != child.Hash {
		t.Fatalf("head after connect = %v", head)
	}
	var bal map[string]string
	must(t, req(t, http.MethodGet, srv.URL+"/v1/accounts/"+a+"/balance", nil), 200, &bal)
	if bal["balance"] != "9" {
		t.Fatalf("mint balance = %s", bal["balance"])
	}
}

func TestTamperedHashRejected(t *testing.T) {
	srv, addr := testServer(t)
	a := addr('a')
	g, _, _ := model.Normalize(model.Block{Height: 0,
		Transactions: []model.Transfer{{From: "", To: a, Amount: "1"}}})
	g.Transactions[0].Amount = "2" // mutate post-hash; claimed hash stale

	raw, _ := json.Marshal(g)
	res, err := http.Post(srv.URL+"/v1/blocks", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	if res.StatusCode != 400 {
		t.Fatalf("status = %d, want 400", res.StatusCode)
	}
	res.Body.Close()

	// Verify endpoint on the empty index: empty chain, match true.
	res, err = http.Post(srv.URL+"/v1/verify/rebuild", "application/json", nil)
	if err != nil {
		t.Fatal(err)
	}
	var rep map[string]any
	must(t, res, 200, &rep)
	if rep["match"] != true {
		t.Fatalf("empty rebuild match = %v", rep)
	}
}

func TestDuplicateDeliveryIdempotent(t *testing.T) {
	srv, addr := testServer(t)
	a := addr('a')
	g, _, _ := model.Normalize(model.Block{Height: 0,
		Transactions: []model.Transfer{{From: "", To: a, Amount: "5"}}})
	raw, _ := json.Marshal(g)

	first, err := http.Post(srv.URL+"/v1/blocks", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	var firstIng map[string]any
	must(t, first, 200, &firstIng)
	if firstIng["accepted"].(float64) != 1 || firstIng["duplicates"].(float64) != 0 {
		t.Fatalf("first delivery: %v", firstIng)
	}

	for i := 0; i < 3; i++ {
		res, err := http.Post(srv.URL+"/v1/blocks", "application/json", bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		var ing map[string]any
		must(t, res, 200, &ing)
		if ing["accepted"].(float64) != 0 || ing["duplicates"].(float64) != 1 {
			t.Fatalf("redelivery %d: accepted=%v duplicates=%v", i, ing["accepted"], ing["duplicates"])
		}
	}
	var bal map[string]string
	must(t, req(t, http.MethodGet, srv.URL+"/v1/accounts/"+a+"/balance", nil), 200, &bal)
	if bal["balance"] != "5" {
		t.Fatalf("balance after redelivery = %s", bal["balance"])
	}
}

func TestCursorCommitsWithBatch(t *testing.T) {
	srv, addr := testServer(t)
	a := addr('a')
	g, _, _ := model.Normalize(model.Block{Height: 0,
		Transactions: []model.Transfer{{From: "", To: a, Amount: "1"}}})
	raw, _ := json.Marshal(g)

	res, err := http.Post(srv.URL+"/v1/blocks?offset=12345", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	must(t, res, 200, nil)
	var state map[string]any
	must(t, req(t, http.MethodGet, srv.URL+"/v1/state", nil), 200, &state)
	if state["streamOffset"].(float64) != 12345 {
		t.Fatalf("offset = %v", state["streamOffset"])
	}

	// An older offset must never move the cursor backwards.
	res, err = http.Post(srv.URL+"/v1/blocks?offset=1", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	must(t, res, 200, nil)
	must(t, req(t, http.MethodGet, srv.URL+"/v1/state", nil), 200, &state)
	if state["streamOffset"].(float64) != 12345 {
		t.Fatalf("cursor moved backwards: %v", state["streamOffset"])
	}
}

func TestUnknownAndBadRequests(t *testing.T) {
	srv, _ := testServer(t)
	must(t, req(t, http.MethodGet, srv.URL+"/v1/blocks/0xdead", nil), 404, nil)
	must(t, req(t, http.MethodGet, srv.URL+"/v1/height/notanint", nil), 400, nil)

	bad := []byte(`{"height":-1}`)
	res, err := http.Post(srv.URL+"/v1/blocks", "application/json", bytes.NewReader(bad))
	if err != nil {
		t.Fatal(err)
	}
	must(t, res, 400, nil)
}

func req(t *testing.T, method, url string, body *bytes.Reader) *http.Response {
	t.Helper()
	var r *http.Request
	var err error
	if body != nil {
		r, err = http.NewRequest(method, url, body)
	} else {
		r, err = http.NewRequest(method, url, nil)
	}
	if err != nil {
		t.Fatal(err)
	}
	res, err := http.DefaultClient.Do(r)
	if err != nil {
		t.Fatal(err)
	}
	return res
}

func mustNDJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return append(b, '\n')
}
