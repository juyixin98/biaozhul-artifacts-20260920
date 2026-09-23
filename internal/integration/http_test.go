package integration

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"forkindexer/internal/domain"
	"forkindexer/internal/httpserver"
	"forkindexer/internal/pgstore"
)

func newHTTPServer(t *testing.T) (*httptest.Server, *pgstore.Store) {
	s := newTestStore(t)
	ts := httptest.NewServer(httpserver.New(s).Handler())
	t.Cleanup(ts.Close)
	return ts, s
}

func postJSON(t *testing.T, ts *httptest.Server, path string, body any) (int, map[string]any) {
	t.Helper()
	buf, _ := json.Marshal(body)
	res, err := http.Post(ts.URL+path, "application/json", bytes.NewReader(buf))
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(res.Body).Decode(&out)
	return res.StatusCode, out
}

func getJSON(t *testing.T, ts *httptest.Server, path string) (int, map[string]any) {
	t.Helper()
	res, err := http.Get(ts.URL + path)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(res.Body).Decode(&out)
	return res.StatusCode, out
}

func envelope(seq int64, b *domain.Block) map[string]any {
	ts := make([]map[string]any, len(b.Transfers))
	for i, tr := range b.Transfers {
		ts[i] = map[string]any{"from": tr.From, "to": tr.To, "amount": tr.Amount}
	}
	return map[string]any{
		"sequence": seq,
		"block": map[string]any{
			"hash":        b.Hash,
			"parent_hash": b.ParentHash,
			"height":      b.Height,
			"transfers":   ts,
		},
	}
}

func TestHTTPEndToEnd(t *testing.T) {
	ts, _ := newHTTPServer(t)
	cb := newChainBuilder(t)
	sc := cb.threeForks()

	if st, _ := getJSON(t, ts, "/healthz"); st != 200 {
		t.Fatal("healthz")
	}
	if st, body := getJSON(t, ts, "/v1/chain/head"); st != 200 || body["head"] != "" {
		t.Fatalf("empty head expected, got %v", body)
	}

	order := []*domain.Block{sc.G, sc.A1, sc.B1, sc.A2, sc.B3, sc.B2, sc.C2}
	for i, blk := range order {
		st, body := postJSON(t, ts, "/v1/blocks", envelope(int64(i+1), blk))
		if st != 200 {
			t.Fatalf("seq %d status %d body %v", i+1, st, body)
		}
	}

	st, head := getJSON(t, ts, "/v1/chain/head")
	if st != 200 || head["height"].(float64) != 3 || head["cum_weight"].(float64) != 4 {
		t.Fatalf("head wrong: %v", head)
	}
	if head["head"].(string) != sc.B3.Hash {
		t.Fatal("final head should be B3")
	}

	st, bal := getJSON(t, ts, "/v1/addresses/"+addrB+"/balance")
	if st != 200 || bal["balance"].(float64) != 1160 {
		t.Fatalf("bob balance: %v", bal)
	}

	st, blk := getJSON(t, ts, "/v1/blocks/"+sc.A2.Hash)
	if st != 200 || blk["in_chain"].(bool) {
		t.Fatalf("A2 must be off-chain: %v", blk)
	}

	st, ver := getJSON(t, ts, "/v1/verify")
	if st != 200 || ver["ok"] != true {
		t.Fatalf("verify: %v", ver)
	}

	// Malformed hash -> 400.
	if st, _ := getJSON(t, ts, "/v1/blocks/0x123"); st != 400 {
		t.Fatalf("expected 400 for malformed hash, got %d", st)
	}
	// Unknown well-formed hash -> 404.
	if st, _ := getJSON(t, ts, "/v1/blocks/0x"+strings.Repeat("f", 64)); st != 404 {
		t.Fatal("expected 404")
	}
}

func TestHTTPBatchAndReplay(t *testing.T) {
	ts, s := newHTTPServer(t)
	cb := newChainBuilder(t)
	sc := cb.threeForks()
	envs := []any{envelope(1, sc.G), envelope(2, sc.B1), envelope(3, sc.B2), envelope(4, sc.B3)}
	buf, _ := json.Marshal(envs)
	res, err := http.Post(ts.URL+"/v1/envelopes", "application/json", bytes.NewReader(buf))
	if err != nil {
		t.Fatal(err)
	}
	if res.StatusCode != 200 {
		var body map[string]any
		_ = json.NewDecoder(res.Body).Decode(&body)
		t.Fatalf("batch status %d %v", res.StatusCode, body)
	}
	res.Body.Close()

	// Replay: same envelopes again, no error and cursor still 4.
	res2, err := http.Post(ts.URL+"/v1/envelopes", "application/json", bytes.NewReader(buf))
	if err != nil {
		t.Fatal(err)
	}
	if res2.StatusCode != 200 {
		t.Fatalf("replay batch status %d", res2.StatusCode)
	}
	res2.Body.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	v, err := s.Head(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if v.Cursor != 4 {
		t.Fatalf("cursor after replay: %d", v.Cursor)
	}
	mustVerifyOK(t, s)
}
