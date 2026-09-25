package httpserver

import (
	"net/http"
	"testing"
)

func TestHTTPGetMissingIs404(t *testing.T) {
	ts, _ := newTestServer(t)
	resp := doReq(t, http.MethodGet, ts.URL+"/resources/nope", nil, "")
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", resp.StatusCode)
	}
}

func TestHTTPBadID(t *testing.T) {
	ts, _ := newTestServer(t)
	// "../etc" is cleaned to /etc by net/http path normalization and
	// therefore never reaches the handler as an id (it 404s).
	for _, id := range []string{"bad id", "%00"} {
		resp := doReq(t, http.MethodGet, ts.URL+"/resources/"+id, nil, "")
		resp.Body.Close()
		if resp.StatusCode != http.StatusBadRequest {
			t.Fatalf("id=%q: expected 400, got %d", id, resp.StatusCode)
		}
	}
}

func TestHTTPMalformedIfMatch(t *testing.T) {
	ts, _ := newTestServer(t)
	resp := doReq(t, http.MethodPut, ts.URL+"/resources/doc",
		map[string]string{"If-Match": "not-a-tag"}, "x")
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("expected 400 for malformed If-Match, got %d", resp.StatusCode)
	}
}

func TestHTTPDeleteMissingRequiresPrecondition(t *testing.T) {
	ts, _ := newTestServer(t)
	resp := doReq(t, http.MethodDelete, ts.URL+"/resources/ghost", nil, "")
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusPreconditionRequired {
		t.Fatalf("delete without If-Match: expected 428, got %d", resp.StatusCode)
	}
}

func TestHTTPAdminStateAndAudit(t *testing.T) {
	ts, _ := newTestServer(t)
	createResource(t, ts.URL, "doc", "v1")

	resp := doReq(t, http.MethodGet, ts.URL+"/admin/state", nil, "")
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("state: %d", resp.StatusCode)
	}

	resp2 := doReq(t, http.MethodGet, ts.URL+"/admin/audit", nil, "")
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusOK {
		t.Fatalf("audit: %d", resp2.StatusCode)
	}

	resp3 := doReq(t, http.MethodGet, ts.URL+"/healthz", nil, "")
	defer resp3.Body.Close()
	if resp3.StatusCode != http.StatusOK {
		t.Fatalf("healthz: %d", resp3.StatusCode)
	}
}

func TestHTTPFaultValidation(t *testing.T) {
	ts, _ := newTestServer(t)
	resp := doReq(t, http.MethodPost, ts.URL+"/admin/faults", nil, `{"failNext":-1}`)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("negative fault: expected 400, got %d", resp.StatusCode)
	}
}

func TestHTTPClockBadInput(t *testing.T) {
	ts, _ := newTestServer(t)
	resp := doReq(t, http.MethodPost, ts.URL+"/admin/clock", nil, `{"set":"not-a-time"}`)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("bad time: expected 400, got %d", resp.StatusCode)
	}
}

func TestHTTPDeleteMissingWithIfMatchIs412(t *testing.T) {
	ts, _ := newTestServer(t)
	for _, h := range []string{"*", `"ghost-v1-deadbeef"`} {
		resp := doReq(t, http.MethodDelete, ts.URL+"/resources/ghost",
			map[string]string{"If-Match": h}, "")
		resp.Body.Close()
		if resp.StatusCode != http.StatusPreconditionFailed {
			t.Fatalf("delete missing with If-Match %s: expected 412, got %d", h, resp.StatusCode)
		}
	}
}
