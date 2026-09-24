package httpapi_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"build-attestation/internal/attestation"
	"build-attestation/internal/httpapi"
	"build-attestation/internal/testkeys"
)

func newTestServer(t *testing.T, now time.Time) *httptest.Server {
	t.Helper()
	cur, old, rev := testkeys.CI()
	other := testkeys.Other()
	t1 := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	t2 := time.Date(2027, 1, 1, 0, 0, 0, 0, time.UTC)
	doc := map[string]any{
		"allowedSourcePrefixes": []string{"https://github.com/example-org/"},
		"builders": []map[string]any{
			{"id": "ci.example.net", "keys": []map[string]any{
				{"id": cur.ID, "publicKey": cur.PublicHex, "notBefore": t1, "notAfter": t2},
				{"id": old.ID, "publicKey": old.PublicHex, "notBefore": time.Date(2025, 1, 1, 0, 0, 0, 0, time.UTC), "notAfter": t1},
				{"id": rev.ID, "publicKey": rev.PublicHex, "notBefore": t1, "notAfter": t2, "revokedAt": time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)},
			}},
			{"id": "other-builder.example.com", "keys": []map[string]any{
				{"id": other.ID, "publicKey": other.PublicHex, "notBefore": t1, "notAfter": t2},
			}},
		},
	}
	raw, _ := json.Marshal(doc)
	policy, err := attestation.ParsePolicy(raw)
	if err != nil {
		t.Fatal(err)
	}
	v, err := attestation.NewVerifier(attestation.Config{
		Policy: policy,
		Now:    func() time.Time { return now },
	})
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(httpapi.New(v).Handler())
	t.Cleanup(srv.Close)
	return srv
}

func validStatement(now time.Time, keyID string) *attestation.Statement {
	return &attestation.Statement{
		Type:      attestation.StatementType,
		BuilderID: "ci.example.net",
		KeyID:     keyID,
		Source: attestation.Source{
			Repository: "https://github.com/example-org/app",
			Commit:     "sha1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		},
		Subjects: []attestation.Digest{{Alg: "sha256",
			Value: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}},
		Params:   map[string]any{"go": "1.22"},
		IssuedAt: now.Format(time.RFC3339),
		Nonce:    "http-nonce-0000000000000001",
	}
}

type respBody struct {
	Accepted bool                `json:"accepted"`
	Code     string              `json:"code"`
	Reason   string              `json:"reason"`
	Result   *attestation.Result `json:"result"`
}

func post(t *testing.T, url string, body []byte) (int, respBody) {
	t.Helper()
	resp, err := http.Post(url+"/verify", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var rb respBody
	if err := json.NewDecoder(resp.Body).Decode(&rb); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return resp.StatusCode, rb
}

func TestHTTPEndToEnd(t *testing.T) {
	now := time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC)
	srv := newTestServer(t, now)
	cur, old, _ := testkeys.CI()

	// health
	hr, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	if hr.StatusCode != 200 {
		t.Fatalf("health: %d", hr.StatusCode)
	}
	hr.Body.Close()

	env, signErr := attestation.Sign(validStatement(now, cur.ID), cur.Private)
	if signErr != nil {
		t.Fatal(signErr)
	}
	raw, _ := json.Marshal(env)

	status, body := post(t, srv.URL, raw)
	if status != http.StatusOK || !body.Accepted {
		t.Fatalf("valid: status=%d body=%+v", status, body)
	}
	if body.Result.Repository != "https://github.com/example-org/app" {
		t.Fatalf("result missing provenance: %+v", body.Result)
	}

	// replay -> 409
	status, body = post(t, srv.URL, raw)
	if status != http.StatusConflict || body.Code != attestation.CodeReplay {
		t.Fatalf("replay: status=%d body=%+v", status, body)
	}

	// old key -> 403
	oldEnv, _ := attestation.Sign(validStatement(now, old.ID), old.Private)
	oldRaw, _ := json.Marshal(oldEnv)
	status, body = post(t, srv.URL, oldRaw)
	if status != http.StatusForbidden || body.Code != attestation.CodeKeyExpired {
		t.Fatalf("old key: status=%d body=%+v", status, body)
	}

	// malformed -> 400
	status, body = post(t, srv.URL, []byte("{not json"))
	if status != http.StatusBadRequest {
		t.Fatalf("malformed: status=%d", status)
	}

	// wrong method -> 405
	pr, _ := http.Post(srv.URL+"/healthz", "application/json", nil)
	pr.Body.Close()
	if pr.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("POST /healthz: got %d", pr.StatusCode)
	}
}
