package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func newTestServer(t *testing.T) (*Server, *http.ServeMux) {
	t.Helper()
	s := New()
	return s, s.Handler().(*http.ServeMux)
}

func doJSON(t *testing.T, h http.Handler, method, target string, body any) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(raw)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, target, rdr)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	if rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatalf("non-JSON response (%d): %s", rec.Code, rec.Body.String())
		}
	}
	return rec.Code, out
}

func createRing(t *testing.T, h http.Handler, body map[string]any) {
	t.Helper()
	code, out := doJSON(t, h, "POST", "/api/rings", body)
	if code != http.StatusCreated {
		t.Fatalf("create ring: status %d body %v", code, out)
	}
}

func TestHealthAndEmptyList(t *testing.T) {
	_, h := newTestServer(t)
	code, out := doJSON(t, h, "GET", "/health", nil)
	if code != 200 || out["status"] != "ok" {
		t.Fatalf("health = %d %v", code, out)
	}
	code, out = doJSON(t, h, "GET", "/api/rings", nil)
	if code != 200 {
		t.Fatalf("list status = %d", code)
	}
	if len(out["rings"].([]any)) != 0 {
		t.Fatalf("expected empty ring list, got %v", out["rings"])
	}
}

func TestCreateGetRoutePlanFlow(t *testing.T) {
	_, h := newTestServer(t)

	createRing(t, h, map[string]any{
		"name":                   "r1",
		"vnodes_per_weight_unit": 8,
		"nodes": []map[string]any{
			{"id": "a", "weight": 1},
			{"id": "b", "weight": 2},
			{"id": "c", "weight": 1},
		},
	})

	// Duplicate name conflicts.
	code, out := doJSON(t, h, "POST", "/api/rings", map[string]any{
		"name":  "r1",
		"nodes": []map[string]any{{"id": "a"}},
	})
	if code != http.StatusConflict {
		t.Fatalf("duplicate create status = %d %v", code, out)
	}

	// Get ring, including vnodes.
	code, out = doJSON(t, h, "GET", "/api/rings/r1?vnodes=true", nil)
	if code != 200 {
		t.Fatalf("get ring status = %d", code)
	}
	ring := out["ring"].(map[string]any)
	if ring["total_vnodes"].(float64) != 32 { // (1+2+1)*8
		t.Fatalf("total vnodes = %v", ring["total_vnodes"])
	}
	if len(out["vnodes"].([]any)) != 32 {
		t.Fatalf("vnodes listing length = %d", len(out["vnodes"].([]any)))
	}

	// GET single-key and multi-key routing.
	code, out = doJSON(t, h, "GET", "/api/rings/r1/route?key=alpha&key=beta", nil)
	if code != 200 {
		t.Fatalf("route GET status = %d %v", code, out)
	}
	results := out["results"].([]any)
	if len(results) != 2 {
		t.Fatalf("route results = %d", len(results))
	}
	first := results[0].(map[string]any)
	if _, ok := first["position_hex"].(string); !ok {
		t.Fatal("position_hex missing")
	}

	code, out = doJSON(t, h, "POST", "/api/rings/r1/route", map[string]any{
		"keys": []string{"k1", "k2", "k3"},
	})
	if code != 200 {
		t.Fatalf("route batch status = %d %v", code, out)
	}
	if len(out["results"].([]any)) != 3 {
		t.Fatal("batch route count wrong")
	}

	// Missing ring -> 404.
	code, _ = doJSON(t, h, "GET", "/api/rings/nope/route?key=x", nil)
	if code != 404 {
		t.Fatalf("missing ring status = %d, want 404", code)
	}

	// Create second topology and ask for a plan by stored names.
	createRing(t, h, map[string]any{
		"name":                   "r2",
		"vnodes_per_weight_unit": 8,
		"nodes": []map[string]any{
			{"id": "a", "weight": 1},
			{"id": "b", "weight": 2},
			{"id": "c", "weight": 1},
			{"id": "d", "weight": 1},
		},
	})
	code, out = doJSON(t, h, "POST", "/api/plan", map[string]any{
		"old":        "r1",
		"new":        "r2",
		"probe_keys": []string{"alpha", "beta", "k1"},
	})
	if code != 200 {
		t.Fatalf("plan status = %d %v", code, out)
	}
	plan := out["plan"].(map[string]any)
	segs := plan["segments"].([]any)
	if len(segs) == 0 {
		t.Fatal("plan has no segments")
	}
	if plan["key_space"] != "18446744073709551616" {
		t.Fatalf("key_space = %v", plan["key_space"])
	}
	probes := out["probes"].([]any)
	if len(probes) != 3 {
		t.Fatalf("probes = %d", len(probes))
	}
}

func TestPlanWithInlineRings(t *testing.T) {
	_, h := newTestServer(t)
	code, out := doJSON(t, h, "POST", "/api/plan", map[string]any{
		"old": map[string]any{
			"nodes":                  []map[string]any{{"id": "a"}, {"id": "b"}},
			"vnodes_per_weight_unit": 8,
		},
		"new": map[string]any{
			"nodes":                  []map[string]any{{"id": "a"}, {"id": "b"}, {"id": "c"}},
			"vnodes_per_weight_unit": 8,
		},
	})
	if code != 200 {
		t.Fatalf("inline plan status = %d %v", code, out)
	}
}

func TestPlanValidation(t *testing.T) {
	_, h := newTestServer(t)

	// Unknown stored ring name.
	code, out := doJSON(t, h, "POST", "/api/plan", map[string]any{
		"old": "ghost",
		"new": map[string]any{"nodes": []map[string]any{{"id": "a"}}},
	})
	if code != 400 {
		t.Fatalf("unknown stored ring status = %d %v", code, out)
	}

	// Invalid inline topology (empty nodes).
	code, out = doJSON(t, h, "POST", "/api/plan", map[string]any{
		"old": map[string]any{"nodes": []map[string]any{}},
		"new": map[string]any{"nodes": []map[string]any{{"id": "a"}}},
	})
	if code != 400 {
		t.Fatalf("invalid topology status = %d %v", code, out)
	}
}

func TestInvalidBodies(t *testing.T) {
	_, h := newTestServer(t)
	req := httptest.NewRequest("POST", "/api/rings", bytes.NewReader([]byte("{not json")))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != 400 {
		t.Fatalf("bad json status = %d", rec.Code)
	}

	code, out := doJSON(t, h, "POST", "/api/rings", map[string]any{"nodes": []map[string]any{{"id": "a"}}})
	if code != 400 || out["error"] == "" {
		t.Fatalf("missing name status = %d %v", code, out)
	}

	// Route validation on an existing ring (nonexistent ring returns 404 first).
	createRing(t, h, map[string]any{
		"name":  "r1",
		"nodes": []map[string]any{{"id": "a"}, {"id": "b"}},
	})
	code, _ = doJSON(t, h, "GET", "/api/rings/r1/route", nil)
	if code != 400 {
		t.Fatalf("route without keys status = %d", code)
	}
}
