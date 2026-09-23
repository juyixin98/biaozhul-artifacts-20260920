package server

import (
	"bytes"
	"crypto/ed25519"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"github.com/example/snapshotprune/internal/crypto"
	"github.com/example/snapshotprune/internal/engine"
	"github.com/example/snapshotprune/internal/types"
)

func setupServer(t *testing.T) (*Server, *engine.Engine, map[string]string) {
	t.Helper()
	dir := t.TempDir()
	privs := map[string]string{}
	g := &types.Genesis{ChainID: "http-1"}
	for _, seed := range []string{"alice", "bob"} {
		k := crypto.DemoPriv(seed)
		a, _ := crypto.AddressFromPub(k.Public().(ed25519.PublicKey))
		g.Allocations = append(g.Allocations, types.GenesisAlloc{Address: a.Hex(), Balance: 5000})
		privs[seed] = a.Hex()
	}
	eng, err := engine.Open(engine.Config{
		DataDir:  filepath.Join(dir, "data"),
		ChainID:  "http-1",
		Genesis:  g,
		LeaseTTL: 50 * time.Millisecond,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = eng.Close() })
	srv := New(eng, "127.0.0.1:0", testLogger())
	return srv, eng, privs
}

func do(t *testing.T, h http.Handler, method, path string, body any) (int, map[string]any) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req := httptest.NewRequest(method, path, rdr)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec.Code, out
}

func TestHTTPRequiresLeaseForHistory(t *testing.T) {
	srv, _, _ := setupServer(t)
	code, body := do(t, srv.httpd.Handler, "GET", "/v1/state", nil)
	if code != http.StatusBadRequest {
		t.Fatalf("expected 400 lease_required, got %d %v", code, body)
	}
}

func TestHTTPLeaseExpiredIsGone(t *testing.T) {
	srv, _, _ := setupServer(t)
	code, body := do(t, srv.httpd.Handler, "POST", "/v1/leases", map[string]uint64{"height": 0})
	if code != http.StatusCreated {
		t.Fatalf("create lease: %d %v", code, body)
	}
	id := body["id"].(string)
	time.Sleep(70 * time.Millisecond)
	code, body = do(t, srv.httpd.Handler, "GET", "/v1/state?lease="+id, nil)
	if code != http.StatusGone || body["code"] != "lease_expired" {
		t.Fatalf("expected 410 lease_expired, got %d %v", code, body)
	}
}

func TestHTTPPrunedHistoryIsGone(t *testing.T) {
	srv, eng, _ := setupServer(t)
	// Append a few blocks via the engine directly.
	appendBlocks(t, eng, 6)
	for _, h := range []uint64{4, 6} {
		if _, err := eng.BuildSnapshot(h); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := eng.Prune(); err != nil {
		t.Fatal(err)
	}
	// Height 2 is now below the reconstructable floor.
	code, body := do(t, srv.httpd.Handler, "POST", "/v1/leases", map[string]uint64{"height": 2})
	if code != http.StatusGone || body["code"] != "history_pruned" {
		t.Fatalf("expected 410 history_pruned, got %d %v", code, body)
	}
	// Height 6 works.
	code, body = do(t, srv.httpd.Handler, "POST", "/v1/leases", map[string]uint64{"height": 6})
	if code != http.StatusCreated {
		t.Fatalf("lease at floor: %d %v", code, body)
	}
}

func TestHTTPSnapshotAndReplay(t *testing.T) {
	srv, eng, _ := setupServer(t)
	appendBlocks(t, eng, 4)

	// Build snapshot at 3.
	code, body := do(t, srv.httpd.Handler, "POST", "/v1/snapshots", map[string]uint64{"height": 3})
	if code != http.StatusCreated {
		t.Fatalf("build snapshot: %d %v", code, body)
	}
	// Duplicate build conflicts.
	code, _ = do(t, srv.httpd.Handler, "POST", "/v1/snapshots", map[string]uint64{"height": 3})
	if code != http.StatusConflict {
		t.Fatalf("expected 409 duplicate, got %d", code)
	}
	// Replay cross-check passes.
	code, body = do(t, srv.httpd.Handler, "POST", "/v1/replay", nil)
	if code != http.StatusOK {
		t.Fatalf("replay: %d %v", code, body)
	}
	if body["matches"] != true {
		t.Fatalf("replay mismatch: %v", body)
	}
}

func TestHTTPRejectBadSignature(t *testing.T) {
	srv, eng, _ := setupServer(t)
	appendBlocks(t, eng, 1)
	tip, _ := eng.Tip()

	alice := crypto.DemoPriv("alice")
	bobPub := crypto.DemoPriv("bob").Public().(ed25519.PublicKey)
	aliceAddr, _ := crypto.AddressFromPub(alice.Public().(ed25519.PublicKey))
	bobAddr, _ := crypto.AddressFromPub(bobPub)

	tx := types.Tx{Nonce: 0, From: aliceAddr, To: bobAddr, Amount: 1, Fee: 0}
	if err := crypto.SignTx(&tx, alice); err != nil {
		t.Fatal(err)
	}
	tx.Sig[0] ^= 0xFF // corrupt signature
	p := map[string]any{
		"height": tip + 1,
		"txs":    []types.Tx{tx},
	}
	code, body := do(t, srv.httpd.Handler, "POST", "/v1/blocks", p)
	if code != http.StatusBadRequest {
		t.Fatalf("expected 400 for bad signature, got %d %v", code, body)
	}
}
