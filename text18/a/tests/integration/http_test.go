package integration

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/google/uuid"

	"sircc/internal/api"
	"sircc/internal/testsupport"
)

type apiClient struct {
	t      *testing.T
	router http.Handler
	key    string
}

func (f *fixture) httpClient(key string) *apiClient {
	return &apiClient{t: f.t, router: api.NewRouter(f.pool, f.svc), key: key}
}

func (c *apiClient) do(method, path, idemKey string, body any) *httptest.ResponseRecorder {
	c.t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	req.Header.Set("Content-Type", "application/json")
	if c.key != "" {
		req.Header.Set("X-API-Key", c.key)
	}
	if idemKey != "" {
		req.Header.Set("Idempotency-Key", idemKey)
	}
	rr := httptest.NewRecorder()
	c.router.ServeHTTP(rr, req)
	return rr
}

func decodeBody(t *testing.T, rr *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(rr.Body.Bytes(), &m); err != nil {
		t.Fatalf("decode %q: %v body=%s", rr.Body.String(), err, rr.Body.String())
	}
	return m
}

func jsonEqual(a, b []byte) bool {
	var x, y any
	if err := json.Unmarshal(a, &x); err != nil {
		return false
	}
	if err := json.Unmarshal(b, &y); err != nil {
		return false
	}
	xa, _ := json.Marshal(x)
	ya, _ := json.Marshal(y)
	return bytes.Equal(xa, ya)
}

// Full HTTP path: auth, create, assign, gated triage and optimistic errors.
func TestHTTP_EndToEndLifecycleAndAuth(t *testing.T) {
	f := newFixture(t)

	anon := f.httpClient("")
	rr := anon.do(http.MethodGet, "/api/v1/incidents", "", nil)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("anon: want 401, got %d", rr.Code)
	}

	admin := f.httpClient(testsupport.APIKeys[testsupport.AdminID])
	analyst := f.httpClient(testsupport.APIKeys[testsupport.Analyst1ID])

	// Analyst creates a P1 case (they automatically become a case member).
	rr = analyst.do(http.MethodPost, "/api/v1/incidents", "", map[string]any{
		"title": "P1 via http", "severity": "P1",
	})
	if rr.Code != http.StatusCreated {
		t.Fatalf("create: want 201, got %d body=%s", rr.Code, rr.Body.String())
	}
	created := decodeBody(t, rr)
	idStr, _ := created["id"].(string)
	if idStr == "" {
		t.Fatalf("missing id: %s", rr.Body.String())
	}
	incidentID := uuid.MustParse(idStr)

	// Analyst triage on P1 before assignment: 422 triage gate.
	rr = analyst.do(http.MethodPost, "/api/v1/incidents/"+incidentID.String()+"/transitions", "", map[string]any{
		"action": "triage", "expected_version": 1,
	})
	if rr.Code != http.StatusUnprocessableEntity {
		t.Fatalf("triage gate: want 422, got %d body=%s", rr.Code, rr.Body.String())
	}

	// Admin assigns responder1.
	rr = admin.do(http.MethodPut,
		"/api/v1/incidents/"+incidentID.String()+"/members/"+testsupport.Responder1ID.String(),
		"", map[string]any{"case_role": "responder"})
	if rr.Code != http.StatusOK {
		t.Fatalf("assign: want 200, got %d body=%s", rr.Code, rr.Body.String())
	}

	// Triage succeeds; repeating the SAME request id replays with header.
	idem := uuid.NewString()
	rr = analyst.do(http.MethodPost, "/api/v1/incidents/"+incidentID.String()+"/transitions", idem, map[string]any{
		"action": "triage", "expected_version": 1,
	})
	if rr.Code != http.StatusOK {
		t.Fatalf("triage: want 200, got %d body=%s", rr.Code, rr.Body.String())
	}
	rr2 := analyst.do(http.MethodPost, "/api/v1/incidents/"+incidentID.String()+"/transitions", idem, map[string]any{
		"action": "triage", "expected_version": 1,
	})
	if rr2.Code != http.StatusOK || rr2.Header().Get("Idempotent-Replay") != "true" {
		t.Fatalf("replay: want 200 + replay header, got %d hdr=%q", rr2.Code, rr2.Header().Get("Idempotent-Replay"))
	}
	if !jsonEqual(rr.Body.Bytes(), rr2.Body.Bytes()) {
		t.Fatalf("replay body differs from original: %s vs %s", rr.Body.String(), rr2.Body.String())
	}

	// Stale version now (case at v2): 409.
	rr = analyst.do(http.MethodPost, "/api/v1/incidents/"+incidentID.String()+"/transitions", "", map[string]any{
		"action": "triage", "expected_version": 1,
	})
	if rr.Code != http.StatusConflict {
		t.Fatalf("stale: want 409, got %d body=%s", rr.Code, rr.Body.String())
	}
}

// Outsider over HTTP is forbidden from a case they cannot see.
func TestHTTP_ForbiddenForOutsider(t *testing.T) {
	f := newFixture(t)
	id := f.createP2Incident(t)

	outsider := f.httpClient(testsupport.APIKeys[testsupport.Analyst2ID])
	rr := outsider.do(http.MethodGet, "/api/v1/incidents/"+id.String(), "", nil)
	if rr.Code != http.StatusForbidden {
		t.Fatalf("outsider read: want 403, got %d body=%s", rr.Code, rr.Body.String())
	}
}
