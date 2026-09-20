package integration

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
)

// Seeded test users. Each test starts from a truncated DB and calls seedUsers.
const (
	uAnalyst   = "t-analyst"
	uResponder = "t-responder"
	uAdmin     = "t-admin"
	uAnalyst2  = "t-analyst-2" // a second analyst who belongs to nothing
)

func seedUsers(t *testing.T) {
	t.Helper()
	rows := []struct{ id, name, role string }{
		{uAnalyst, "Ana", "analyst"},
		{uResponder, "Res", "responder"},
		{uAdmin, "Adm", "admin"},
		{uAnalyst2, "Outsider", "analyst"},
	}
	for _, u := range rows {
		_, err := pool.Exec(context.Background(),
			`INSERT INTO users (id, name, role) VALUES ($1,$2,$3)`, u.id, u.name, u.role)
		if err != nil {
			t.Fatalf("seed user %s: %v", u.id, err)
		}
	}
}

type incidentBody struct {
	ID      string `json:"id"`
	Stage   string `json:"stage"`
	Version int64  `json:"version"`
}

func apiCreateIncident(t *testing.T, h http.Handler, creator, severity, title string) incidentBody {
	t.Helper()
	body := fmt.Sprintf(`{"title":%q,"severity":%q}`, title, severity)
	w := doJSON(t, h, http.MethodPost, "/v1/incidents", creator, reqID(), body)
	if w.Code != http.StatusOK {
		t.Fatalf("create incident: status=%d body=%s", w.Code, w.Body.String())
	}
	var got incidentBody
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode create: %v body=%s", err, w.Body.String())
	}
	return got
}

// transitionAs posts a transition, optionally with extra JSON fields, and
// returns the raw recorder for assertion on code/body.
func transitionAs(t *testing.T, h http.Handler, user, incidentID string, version int64, extra string) *httptest.ResponseRecorder {
	t.Helper()
	body := `{"expected_version":` + strconv.FormatInt(version, 10)
	if extra != "" {
		body += "," + extra
	}
	body += "}"
	return doJSON(t, h, http.MethodPost, "/v1/incidents/"+incidentID+"/transition", user, reqID(), body)
}

// transitionSameRequest is like transitionAs but with an explicit request id,
// used for idempotency replay tests.
func transitionSameRequest(t *testing.T, h http.Handler, user, incidentID, requestID string, version int64, extra string) *httptest.ResponseRecorder {
	t.Helper()
	body := `{"expected_version":` + strconv.FormatInt(version, 10)
	if extra != "" {
		body += "," + extra
	}
	body += "}"
	return doJSON(t, h, http.MethodPost, "/v1/incidents/"+incidentID+"/transition", user, requestID, body)
}

func decodeBody(t *testing.T, w *httptest.ResponseRecorder, dst any) {
	t.Helper()
	if err := json.Unmarshal(w.Body.Bytes(), dst); err != nil {
		t.Fatalf("decode body %q: %v", w.Body.String(), err)
	}
}
