package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const validBody = `{
  "cluster": {
    "devices": [
      {"id": "gpu0", "memory_mb": 80000, "used_memory_mb": 0,     "numa_node": 0},
      {"id": "gpu1", "memory_mb": 80000, "used_memory_mb": 0,     "numa_node": 0},
      {"id": "gpu2", "memory_mb": 80000, "used_memory_mb": 0,     "numa_node": 0},
      {"id": "gpu3", "memory_mb": 80000, "used_memory_mb": 0,     "numa_node": 0},
      {"id": "gpu4", "memory_mb": 80000, "used_memory_mb": 0,     "numa_node": 1},
      {"id": "gpu5", "memory_mb": 80000, "used_memory_mb": 0,     "numa_node": 1},
      {"id": "gpu6", "memory_mb": 80000, "used_memory_mb": 0,     "numa_node": 1},
      {"id": "gpu7", "memory_mb": 80000, "used_memory_mb": 70000, "numa_node": 1}
    ],
    "links": [],
    "default_link_cost": {"same_numa_cost": 1, "cross_numa_cost": 10}
  },
  "task": {"name": "llm-training", "replicas": 4, "memory_per_replica_mb": 20000}
}`

func postPlacement(t *testing.T, body string) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	h := &Handler{}
	req := httptest.NewRequest(http.MethodPost, "/api/v1/placement", strings.NewReader(body))
	rec := httptest.NewRecorder()
	h.placement(rec, req)
	var parsed map[string]any
	if rec.Body.Len() > 0 {
		_ = json.Unmarshal(rec.Body.Bytes(), &parsed)
	}
	return rec, parsed
}

func TestHealthz(t *testing.T) {
	h := &Handler{}
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	h.healthz(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("code = %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"ok"`) {
		t.Fatalf("body = %s", rec.Body.String())
	}
}

func TestPlacementOptimal(t *testing.T) {
	rec, body := postPlacement(t, validBody)
	if rec.Code != http.StatusOK {
		t.Fatalf("code = %d body = %s", rec.Code, rec.Body.String())
	}
	if body["status"] != "optimal" {
		t.Fatalf("status = %v", body["status"])
	}
	if cost, _ := body["total_communication_cost"].(float64); cost != 6 {
		t.Errorf("cost = %v, want 6", body["total_communication_cost"])
	}
	placement, _ := body["placement"].([]any)
	if len(placement) != 4 {
		t.Fatalf("placement len = %d", len(placement))
	}
	wantIDs := []string{"gpu0", "gpu1", "gpu2", "gpu3"}
	for i, p := range placement {
		pm, _ := p.(map[string]any)
		if pm["device_id"] != wantIDs[i] {
			t.Errorf("placement[%d] = %v, want %s", i, pm["device_id"], wantIDs[i])
		}
		if int(pm["replica_index"].(float64)) != i {
			t.Errorf("replica_index mismatch at %d", i)
		}
	}
	diag, _ := body["diagnostics"].(map[string]any)
	if diag["strategy"] != "exhaustive-enumeration" {
		t.Errorf("strategy = %v", diag["strategy"])
	}
	pairs, _ := body["pairs"].([]any)
	if len(pairs) != 6 {
		t.Errorf("pairs = %d, want 6", len(pairs))
	}
}

func TestPlacementRejectedFragmentation(t *testing.T) {
	body := `{
      "cluster": {
        "devices": [
          {"id": "gpu0", "memory_mb": 40000, "used_memory_mb": 30000, "numa_node": 0},
          {"id": "gpu1", "memory_mb": 40000, "used_memory_mb": 20000, "numa_node": 0}
        ]
      },
      "task": {"name": "huge", "replicas": 2, "memory_per_replica_mb": 25000}
    }`
	rec, parsed := postPlacement(t, body)
	if rec.Code != http.StatusOK {
		t.Fatalf("code = %d", rec.Code)
	}
	if parsed["status"] != "infeasible" {
		t.Fatalf("status = %v", parsed["status"])
	}
	if parsed["reason"] != "MEMORY_TOO_LARGE" {
		t.Fatalf("reason = %v", parsed["reason"])
	}
	rejected, _ := parsed["rejected"].(map[string]any)
	if rejected == nil {
		t.Fatal("rejected detail missing")
	}
	if rejected["largest_free_mb"].(float64) != 20000 {
		t.Errorf("largest_free_mb = %v", rejected["largest_free_mb"])
	}
	if rejected["eligible_devices"].(float64) != 0 {
		t.Errorf("eligible_devices = %v", rejected["eligible_devices"])
	}
	if _, ok := parsed["placement"]; ok {
		t.Error("placement should be omitted on rejection")
	}
}

func TestPlacementRejectedInsufficientDevices(t *testing.T) {
	body := strings.Replace(validBody, `"replicas": 4`, `"replicas": 9`, 1)
	_, parsed := postPlacement(t, body)
	if parsed["status"] != "infeasible" || parsed["reason"] != "INSUFFICIENT_ELIGIBLE_DEVICES" {
		t.Fatalf("status=%v reason=%v", parsed["status"], parsed["reason"])
	}
	rejected, _ := parsed["rejected"].(map[string]any)
	if rejected["eligible_devices"].(float64) != 7 {
		t.Errorf("eligible_devices = %v, want 7", rejected["eligible_devices"])
	}
}

func TestInvalidJSON(t *testing.T) {
	rec, parsed := postPlacement(t, `{"cluster":`)
	if rec.Code != http.StatusBadRequest || parsed["reason"] != "INVALID_REQUEST" {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
}

func TestUnknownFieldRejected(t *testing.T) {
	rec, _ := postPlacement(t, strings.Replace(validBody, `"task": {`, `"bogus": 1, "task": {`, 1))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("code = %d, want 400", rec.Code)
	}
}

func TestMissingReplicas(t *testing.T) {
	body := strings.Replace(validBody, `"replicas": 4, `, "", 1)
	rec, parsed := postPlacement(t, body)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("code = %d", rec.Code)
	}
	if !strings.Contains(parsed["error"].(string), "replicas") {
		t.Errorf("error = %v", parsed["error"])
	}
}

func TestLinkReferencesUnknownDevice(t *testing.T) {
	body := strings.Replace(validBody, `"links": []`,
		`"links": [{"a": "gpu0", "b": "gpuX", "cost": 5}]`, 1)
	rec, parsed := postPlacement(t, body)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("code = %d", rec.Code)
	}
	if !strings.Contains(parsed["error"].(string), "unknown device") {
		t.Errorf("error = %v", parsed["error"])
	}
}

func TestMethodNotAllowed(t *testing.T) {
	h := &Handler{}
	req := httptest.NewRequest(http.MethodGet, "/api/v1/placement", nil)
	rec := httptest.NewRecorder()
	h.placement(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("code = %d", rec.Code)
	}
}

func TestBodyTooLarge(t *testing.T) {
	h := &Handler{}
	big := bytes.Repeat([]byte("a"), maxBodyBytes+10)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/placement", bytes.NewReader(big))
	rec := httptest.NewRecorder()
	h.placement(rec, req)
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("code = %d", rec.Code)
	}
}

func TestNotFound(t *testing.T) {
	h := &Handler{}
	req := httptest.NewRequest(http.MethodGet, "/nope", nil)
	rec := httptest.NewRecorder()
	h.index(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("code = %d", rec.Code)
	}
}
