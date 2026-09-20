package integration

import (
	"net/http"
	"testing"
)

// TestUnauthenticatedRejected: missing X-User-Id is 401.
func TestUnauthenticatedRejected(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	w := doJSON(t, h, http.MethodGet, "/v1/incidents", "", "", "")
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("no user header want 401, got %d", w.Code)
	}
}

// TestUnknownUserRejected: an unrecognized user id is 401.
func TestUnknownUserRejected(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	w := doJSON(t, h, http.MethodGet, "/v1/incidents", "ghost", "", "")
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("unknown user want 401, got %d", w.Code)
	}
}

// TestMissingRequestIDRejected: mutating calls require an idempotency key.
func TestMissingRequestIDRejected(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	w := doJSON(t, h, http.MethodPost, "/v1/incidents", uAnalyst, "",
		`{"title":"x","severity":"P2"}`)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("missing X-Request-Id want 400, got %d", w.Code)
	}
}

// TestNonMemberCannotAccessCase: a user who is not a member is denied on
// every incident-scoped endpoint.
func TestNonMemberCannotAccessCase(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "private case")

	endpoints := []struct {
		method string
		path   string
		body   string
	}{
		{http.MethodGet, "/v1/incidents/" + inc.ID, ""},
		{http.MethodGet, "/v1/incidents/" + inc.ID + "/events", ""},
		{http.MethodGet, "/v1/incidents/" + inc.ID + "/evidence", ""},
		{http.MethodGet, "/v1/incidents/" + inc.ID + "/action-items", ""},
		{http.MethodGet, "/v1/incidents/" + inc.ID + "/export", ""},
		{http.MethodPost, "/v1/incidents/" + inc.ID + "/evidence", `{"content":"x"}`},
		{http.MethodPost, "/v1/incidents/" + inc.ID + "/action-items",
			`{"title":"t","owner_id":"` + uResponder + `","due_at":"2030-01-01T00:00:00Z"}`},
	}
	for _, e := range endpoints {
		w := doJSON(t, h, e.method, e.path, uAnalyst2, reqID(), e.body)
		if w.Code != http.StatusForbidden {
			t.Fatalf("%s %s as outsider: want 403, got %d %s",
				e.method, e.path, w.Code, w.Body.String())
		}
	}

	// Non-member also cannot list the case.
	w := doJSON(t, h, http.MethodGet, "/v1/incidents", uAnalyst2, "", "")
	var list struct {
		Incidents []struct {
			ID string `json:"id"`
		} `json:"incidents"`
	}
	decodeBody(t, w, &list)
	for _, c := range list.Incidents {
		if c.ID == inc.ID {
			t.Fatal("outsider can see a case they don't belong to in the list")
		}
	}
}

// TestOnlyAdminAssigns: analyst and responder cannot assign people.
func TestOnlyAdminAssigns(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "assignment case")
	body := `{"user_id":"` + uResponder + `","role":"responder"}`

	for _, user := range []string{uAnalyst, uResponder} {
		w := doJSON(t, h, http.MethodPost, "/v1/incidents/"+inc.ID+"/assignments",
			user, reqID(), body)
		if w.Code != http.StatusForbidden {
			t.Fatalf("assignment by %s want 403, got %d", user, w.Code)
		}
	}
}

// TestResponderCannotAddEvidence: evidence is analyst-only.
func TestResponderCannotAddEvidence(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "ev perms")
	// Make the responder a member so scope alone would allow it; the role
	// check must still deny evidence authoring.
	assignResponder(t, h, inc.ID)

	w := doJSON(t, h, http.MethodPost, "/v1/incidents/"+inc.ID+"/evidence",
		uResponder, reqID(), `{"content":"should be denied"}`)
	if w.Code != http.StatusForbidden {
		t.Fatalf("responder evidence want 403, got %d %s", w.Code, w.Body.String())
	}
}

// TestNonAdminCannotReadGlobalAudit: audit trail is scoped.
func TestNonAdminCannotReadGlobalAudit(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	w := doJSON(t, h, http.MethodGet, "/v1/audit-events", uResponder, "", "")
	if w.Code != http.StatusForbidden {
		t.Fatalf("global audit by responder want 403, got %d", w.Code)
	}

	// Admin is allowed.
	w = doJSON(t, h, http.MethodGet, "/v1/audit-events", uAdmin, "", "")
	if w.Code != http.StatusOK {
		t.Fatalf("global audit by admin want 200, got %d", w.Code)
	}
}

// TestAssignedPersonGainsScope: after admin assignment the responder can read
// the case (assignment grants membership).
func TestAssignedPersonGainsScope(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "scope grant")

	// Before assignment: forbidden.
	if w := doJSON(t, h, http.MethodGet, "/v1/incidents/"+inc.ID, uResponder, "", ""); w.Code != http.StatusForbidden {
		t.Fatalf("pre-assignment read want 403, got %d", w.Code)
	}
	assignResponder(t, h, inc.ID)
	// After: allowed.
	if w := doJSON(t, h, http.MethodGet, "/v1/incidents/"+inc.ID, uResponder, "", ""); w.Code != http.StatusOK {
		t.Fatalf("post-assignment read want 200, got %d %s", w.Code, w.Body.String())
	}
}
