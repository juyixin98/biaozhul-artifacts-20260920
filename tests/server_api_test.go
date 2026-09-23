package tests

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"ximbox/internal/server"
)

type api struct {
	t      *testing.T
	client *http.Client
	base   string
}

func newAPI(t *testing.T, h *harness) *api {
	srv := httptest.NewServer(server.New(h.svc, h.st))
	t.Cleanup(srv.Close)
	return &api{t: t, client: srv.Client(), base: srv.URL}
}

func (a *api) post(path string, body any) (int, map[string]any) {
	a.t.Helper()
	raw, _ := json.Marshal(body)
	resp, err := a.client.Post(a.base+path, "application/json", bytes.NewReader(raw))
	if err != nil {
		a.t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	var out map[string]any
	_ = json.Unmarshal(data, &out)
	if out == nil {
		out = map[string]any{"raw": string(data)}
	}
	return resp.StatusCode, out
}

func (a *api) get(path string) (int, map[string]any) {
	a.t.Helper()
	resp, err := a.client.Get(a.base + path)
	if err != nil {
		a.t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	var out map[string]any
	_ = json.Unmarshal(data, &out)
	if out == nil {
		out = map[string]any{"raw": string(data)}
	}
	return resp.StatusCode, out
}

// TestHealthAndRouting covers basic liveness and 404.
func TestHealthAndRouting(t *testing.T) {
	h := newHarness(t)
	a := newAPI(t, h)
	if code, body := a.get("/healthz"); code != http.StatusOK || body["status"] != "ok" {
		t.Fatalf("healthz code=%d body=%v", code, body)
	}
	if code, _ := a.get("/v1/nope"); code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", code)
	}
}

// TestAPIInvalidSignatureRejected maps a real signature failure to HTTP 401.
func TestAPIInvalidSignatureRejected(t *testing.T) {
	h := newHarness(t)
	a := newAPI(t, h)
	const chain, ch = "chainA", "http-auth"
	h.propose(chain, 1)

	env := h.signMsg(chain, ch, 0, 1, transferBody("alice", 10))
	sig := []byte(env.Signature)
	if sig[len(sig)-1] == '0' {
		sig[len(sig)-1] = '1'
	} else {
		sig[len(sig)-1] = '0'
	}
	env.Signature = string(sig)
	code, body := a.post("/v1/messages", env)
	if code != http.StatusUnauthorized {
		t.Fatalf("status=%d body=%v, want 401", code, body)
	}
}

// TestAPIBlockFinalConflict maps a final-height replacement attempt to 409.
func TestAPIBlockFinalConflict(t *testing.T) {
	h := newHarness(t)
	a := newAPI(t, h)
	const chain = "chainA"
	hash := h.blockHash(chain, 1)
	if code, _ := a.post("/v1/blocks", map[string]any{
		"source_chain": chain, "height": 1, "hash": hash,
		"parent_hash": "0x0000000000000000000000000000000000000000000000000000000000000000",
	}); code != http.StatusCreated {
		t.Fatalf("genesis proposal code=%d want 201", code)
	}
	if code, _ := a.post("/v1/blocks/confirm", map[string]any{
		"source_chain": chain, "hash": hash,
	}); code != http.StatusOK {
		t.Fatalf("confirm code=%d want 200", code)
	}
	evil := h.competingHash(chain, 1, "evil")
	if code, _ := a.post("/v1/blocks", map[string]any{
		"source_chain": chain, "height": 1, "hash": evil,
		"parent_hash": "0x0000000000000000000000000000000000000000000000000000000000000000",
	}); code != http.StatusConflict {
		t.Fatalf("replacing final height code=%d want 409", code)
	}
}

// TestAPIConflictingCopyFreezesAndReturnsEvidence exercises the full HTTP
// happy path then the conflict path and inspects /evidence and /alerts.
func TestAPIConflictingCopyFreezesAndReturnsEvidence(t *testing.T) {
	h := newHarness(t)
	a := newAPI(t, h)
	const chain, ch = "chainA", "http-conflict"
	h.propose(chain, 1)
	h.confirm(chain, 1)

	send := func(seq uint64, to string, amount int64) (int, map[string]any) {
		env := h.signMsg(chain, ch, seq, 1, transferBody(to, amount))
		return a.post("/v1/messages", env)
	}

	if code, body := send(0, "alice", 10); code != http.StatusCreated {
		t.Fatalf("seq0 code=%d body=%v", code, body)
	}
	// Same digest again: idempotent, channel stays active.
	if code, body := send(0, "alice", 10); code != http.StatusCreated {
		t.Fatalf("duplicate code=%d body=%v", code, body)
	}
	// Different digest for the same key: evidence + freeze.
	code, body := send(0, "mallory", 777)
	if code != http.StatusCreated {
		t.Fatalf("conflict ingestion should be recorded, got %d %v", code, body)
	}
	if body["channel_frozen"] != true {
		t.Fatalf("expected channel_frozen=true, got %v", body["channel_frozen"])
	}
	// Further messages rejected 409.
	if code, _ := send(1, "bob", 1); code != http.StatusConflict {
		t.Fatalf("frozen channel ingest code=%d, want 409", code)
	}

	code, ev := a.get("/v1/evidence?source_chain=" + chain + "&channel=" + ch)
	if code != http.StatusOK {
		t.Fatalf("evidence code=%d", code)
	}
	list, _ := ev["evidence"].([]any)
	if len(list) != 1 {
		t.Fatalf("expected 1 evidence record, got %d", len(list))
	}
	code, al := a.get("/v1/alerts?source_chain=" + chain + "&channel=" + ch)
	if code != http.StatusOK {
		t.Fatalf("alerts code=%d", code)
	}
	alertList, _ := al["alerts"].([]any)
	if len(alertList) != 1 {
		t.Fatalf("expected 1 alert, got %d", len(alertList))
	}
	first := alertList[0].(map[string]any)
	if first["kind"] != "equivocation_executed" {
		t.Fatalf("alert kind=%v", first["kind"])
	}

	code, accts := a.get("/v1/accounts")
	if code != http.StatusOK {
		t.Fatalf("accounts code=%d", code)
	}
	rows, _ := accts["accounts"].([]any)
	found := false
	for _, r := range rows {
		row := r.(map[string]any)
		if row["address"] == "alice" {
			found = true
			if bal, _ := row["balance"].(float64); bal != 10 {
				t.Fatalf("alice balance=%v want 10", row["balance"])
			}
		}
		if row["address"] == "mallory" {
			t.Fatal("conflict copy must never credit mallory")
		}
	}
	if !found {
		t.Fatal("alice account missing")
	}
}

// TestAPIBlockRejectsUnknownChainAndBadHash exercises request validation.
func TestAPIBlockRejectsBadInput(t *testing.T) {
	h := newHarness(t)
	a := newAPI(t, h)
	if code, _ := a.post("/v1/blocks", map[string]any{
		"source_chain": "chainZ", "height": 1, "hash": h.blockHash("chainA", 1),
		"parent_hash": "0x" + string(make([]byte, 64)),
	}); code != http.StatusBadRequest {
		t.Fatalf("unknown chain code=%d want 400", code)
	}
}
