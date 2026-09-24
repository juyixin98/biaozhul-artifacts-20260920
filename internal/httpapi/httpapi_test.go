package httpapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func doTransfer(t *testing.T, srv *httptest.Server, body string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Post(srv.URL+"/api/transfers", "application/json", bytes.NewBufferString(body))
	if err != nil {
		t.Fatalf("POST: %v", err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	return resp.StatusCode, out
}

func TestTransferEndpoint(t *testing.T) {
	srv := httptest.NewServer(NewHandler())
	defer srv.Close()

	status, out := doTransfer(t, srv, `{
		"sizeBytes": 65536, "seed": 42,
		"dropRate": 0.1, "dupRate": 0.05, "reorderRate": 0.1,
		"stalePackets": 3, "window": 8, "chunkSize": 1024, "rtoMs": 10
	}`)
	if status != http.StatusOK {
		t.Fatalf("status = %d, body = %v", status, out)
	}
	if out["ok"] != true || out["hashMatch"] != true {
		t.Fatalf("transfer failed: %v", out)
	}
	if out["bytes"].(float64) != 65536 {
		t.Fatalf("bytes = %v, want 65536", out["bytes"])
	}
	sender := out["sender"].(map[string]any)
	if sender["maxInFlight"].(float64) > 8 {
		t.Fatalf("maxInFlight %v exceeds window 8", sender["maxInFlight"])
	}
	if sender["retransmits"].(float64) == 0 {
		t.Fatal("expected retransmissions under 10% loss")
	}
}

func TestTransferEndpointRejectsBadConfig(t *testing.T) {
	srv := httptest.NewServer(NewHandler())
	defer srv.Close()

	status, out := doTransfer(t, srv, `{"sizeBytes": 1024, "dropRate": 0.99}`)
	if status != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400; body = %v", status, out)
	}
	if out["error"] == nil || out["error"] == "" {
		t.Fatalf("expected error message, got %v", out)
	}
}

func TestTransferEndpointRejectsBadJSON(t *testing.T) {
	srv := httptest.NewServer(NewHandler())
	defer srv.Close()

	status, _ := doTransfer(t, srv, `{not json`)
	if status != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", status)
	}
}

func TestHealthEndpoint(t *testing.T) {
	srv := httptest.NewServer(NewHandler())
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/api/health")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
}
