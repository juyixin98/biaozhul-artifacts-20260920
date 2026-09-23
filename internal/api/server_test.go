package api_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"mirror-admission/internal/api"
	"mirror-admission/internal/policy"
	"mirror-admission/internal/service"
	"mirror-admission/internal/store"
	"mirror-admission/internal/testkit"
)

func setup(t *testing.T) (http.Handler, *service.Service, testkit.Keys) {
	t.Helper()
	k := testkit.GenerateKeys(t)
	pol, hash, err := policy.LoadAndVerify("../../policy/admission.rego", "../../policy/policy_freeze.lock.json")
	if err != nil {
		t.Fatal(err)
	}
	eng, err := policy.NewEngine(pol, hash)
	if err != nil {
		t.Fatal(err)
	}
	svc := service.New(eng, k.Trusted(), testkit.Allowlist(), store.NewMemory()).
		WithClock(testkit.FixedClock{T: time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC)})
	return api.NewServer(svc), svc, k
}

func do(t *testing.T, h http.Handler, method, path string, body any) (int, map[string]any) {
	t.Helper()
	var r *http.Request
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		r = httptest.NewRequest(method, path, bytes.NewReader(b))
		r.Header.Set("Content-Type", "application/json")
	} else {
		r = httptest.NewRequest(method, path, nil)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	out := map[string]any{}
	if w.Body.Len() > 0 {
		if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
			t.Fatalf("non-json response %q: %v", w.Body.String(), err)
		}
	}
	return w.Code, out
}

func TestHealthAndMeta(t *testing.T) {
	h, _, _ := setup(t)
	if code, body := do(t, h, "GET", "/healthz", nil); code != http.StatusOK || body["status"] != "ok" {
		t.Fatalf("health: %d %v", code, body)
	}
	code, meta := do(t, h, "GET", "/v1/meta", nil)
	if code != http.StatusOK {
		t.Fatalf("meta code %d", code)
	}
	if meta["policy_version"] != "1.4.0" {
		t.Fatalf("unfrozen meta: %v", meta["policy_version"])
	}
	if len(meta["allowed_base_images"].([]any)) != 1 {
		t.Fatal("allowlist not exposed")
	}
}

func TestEvaluateCreatesReportAndFetchesIt(t *testing.T) {
	h, _, k := setup(t)
	cfg := testkit.MustMarshal(t, testkit.Config(map[string]any{"User": "10001", "Privileged": false}))
	sbom, _ := testkit.SBOM(t, cfg, testkit.AllowBase)
	att := testkit.Attest(t, k, cfg, sbom, "pass")

	req := map[string]any{
		"image_config":      json.RawMessage(cfg),
		"sbom":              json.RawMessage(sbom),
		"attestation":       json.RawMessage(att),
		"allowlist_version": testkit.Allowlist().Version,
	}
	code, body := do(t, h, "POST", "/v1/admission/evaluate", req)
	if code != http.StatusCreated {
		t.Fatalf("want 201, got %d: %v", code, body)
	}
	if body["decision"] != "ALLOW" {
		t.Fatalf("want ALLOW got %v", body["decision"])
	}
	id, _ := body["id"].(string)
	if !strings.HasPrefix(id, "rpt_") {
		t.Fatalf("bad report id %q", id)
	}

	// GET the immutable report back.
	loc := "/v1/reports/" + id
	code, got := do(t, h, "GET", loc, nil)
	if code != http.StatusOK || got["id"] != id {
		t.Fatalf("fetch report: %d %v", code, got)
	}

	// Location header is set on creation (prefix check; ids are random).
	r := httptest.NewRequest("POST", "/v1/admission/evaluate",
		bytes.NewReader(mustJSON(t, req)))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if !strings.HasPrefix(w.Header().Get("Location"), "/v1/reports/rpt_") {
		t.Fatalf("missing Location header: %q", w.Header().Get("Location"))
	}
}

func TestUnknownFieldRejected(t *testing.T) {
	h, _, k := setup(t)
	cfg := testkit.MustMarshal(t, testkit.Config(map[string]any{"User": "10001", "Privileged": false}))
	sbom, _ := testkit.SBOM(t, cfg, testkit.AllowBase)
	att := testkit.Attest(t, k, cfg, sbom, "pass")
	body := map[string]any{
		"image_config":         json.RawMessage(cfg),
		"sbom":                 json.RawMessage(sbom),
		"attestation":          json.RawMessage(att),
		"allowlist_version":    testkit.Allowlist().Version,
		"silent_default_allow": true, // unknown field must be rejected
	}
	code, resp := do(t, h, "POST", "/v1/admission/evaluate", body)
	if code != http.StatusBadRequest {
		t.Fatalf("unknown field must be 400, got %d %v", code, resp)
	}
}

func TestReportsList(t *testing.T) {
	h, svc, k := setup(t)
	cfg := testkit.MustMarshal(t, testkit.Config(map[string]any{"User": "10001", "Privileged": false}))
	sbom, _ := testkit.SBOM(t, cfg, testkit.AllowBase)
	att := testkit.Attest(t, k, cfg, sbom, "pass")
	req := map[string]any{
		"image_config":      json.RawMessage(cfg),
		"sbom":              json.RawMessage(sbom),
		"attestation":       json.RawMessage(att),
		"allowlist_version": testkit.Allowlist().Version,
	}
	for i := 0; i < 2; i++ {
		if code, _ := do(t, h, "POST", "/v1/admission/evaluate", req); code != http.StatusCreated {
			t.Fatalf("evaluate %d failed", i)
		}
	}
	code, body := do(t, h, "GET", "/v1/reports", nil)
	if code != http.StatusOK {
		t.Fatalf("list code %d", code)
	}
	reps := body["reports"].([]any)
	if len(reps) != 2 {
		t.Fatalf("want 2 reports, got %d", len(reps))
	}

	// Unknown id -> 404.
	code, _ = do(t, h, "GET", "/v1/reports/rpt_nonexistent", nil)
	if code != http.StatusNotFound {
		t.Fatalf("want 404 got %d", code)
	}

	// Sanity: store wiring is the same service.
	list, _ := svc.ListReports(context.Background())
	if len(list) != 2 {
		t.Fatalf("store has %d reports", len(list))
	}
}

func TestLabelDriftOverHTTP(t *testing.T) {
	h, _, k := setup(t)
	orig := testkit.MustMarshal(t, testkit.Config(map[string]any{"User": "10001", "Privileged": false}))
	sbomOrig, _ := testkit.SBOM(t, orig, testkit.AllowBase)
	att := testkit.Attest(t, k, orig, sbomOrig, "pass")

	drifted := testkit.MustMarshal(t, testkit.Config(map[string]any{
		"User": "root", "Privileged": false,
		"Labels": map[string]any{"quay.io/retag": "victim:1.0"},
	}))
	sbomDrift, _ := testkit.SBOM(t, drifted, testkit.AllowBase)
	req := map[string]any{
		"image_config":      json.RawMessage(drifted),
		"sbom":              json.RawMessage(sbomDrift),
		"attestation":       json.RawMessage(att), // old signed evidence
		"allowlist_version": testkit.Allowlist().Version,
	}
	code, body := do(t, h, "POST", "/v1/admission/evaluate", req)
	if code != http.StatusCreated {
		t.Fatalf("code %d: %v", code, body)
	}
	if body["decision"] != "DENY" {
		t.Fatalf("drifted image with old attestation must DENY, got %v", body["decision"])
	}
}

func mustJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}
