package integration

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"vci/internal/clock"
	"vci/internal/httpapi"
	"vci/internal/service"
)

func newHTTPServer(t *testing.T) (*httptest.Server, *service.Service) {
	t.Helper()
	st := newTestStore(t)
	svc := service.New(st, clock.System{})
	srv := httptest.NewServer(httpapi.NewServer(svc).Router())
	t.Cleanup(srv.Close)
	return srv, svc
}

func postJSON(t *testing.T, url string, body any) (int, map[string]any) {
	t.Helper()
	return doJSON(t, http.MethodPost, url, body)
}

func doJSON(t *testing.T, method, url string, body any) (int, map[string]any) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, url, rdr)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out map[string]any
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &out); err != nil {
			t.Fatalf("non-json response %d: %s", resp.StatusCode, raw)
		}
	}
	return resp.StatusCode, out
}

func TestHTTPEndToEnd(t *testing.T) {
	srv, _ := newHTTPServer(t)

	// Health and snapshot.
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("healthz %d", resp.StatusCode)
	}

	// Create issuer.
	st, body := postJSON(t, srv.URL+"/v1/issuers", map[string]any{"name": "http-ca"})
	if st != http.StatusCreated {
		t.Fatalf("create issuer: %d %v", st, body)
	}
	issuerID := body["issuer"].(map[string]any)["issuer_id"].(string)

	// Issue credential.
	exp := time.Now().Add(time.Hour).Format(time.RFC3339Nano)
	st, body = postJSON(t, srv.URL+"/v1/credentials", map[string]any{
		"issuer_id":  issuerID,
		"subject":    "alice",
		"purpose":    "door-access",
		"expires_at": exp,
		"content":    map[string]any{"zone": "north", "level": 4},
	})
	if st != http.StatusCreated {
		t.Fatalf("issue: %d %v", st, body)
	}
	credID := body["credential_id"].(string)
	if body["signature_b64"] == nil || body["payload_b64"] == nil {
		t.Fatal("missing signature/payload in response")
	}
	issueBody := body

	// Verify VALID.
	st, body = postJSON(t, srv.URL+"/v1/verify", map[string]any{
		"credential_id": credID, "expected_purpose": "door-access",
		"content": map[string]any{"zone": "north", "level": 4},
	})
	if st != http.StatusOK || body["verdict"] != "VALID" {
		t.Fatalf("verify: %d %v", st, body)
	}

	// Purpose mismatch.
	st, body = postJSON(t, srv.URL+"/v1/verify", map[string]any{
		"credential_id": credID, "expected_purpose": "vpn",
	})
	if st != http.StatusOK || body["verdict"] != "PURPOSE_MISMATCH" {
		t.Fatalf("purpose: %d %v", st, body)
	}

	// Content mismatch.
	st, body = postJSON(t, srv.URL+"/v1/verify", map[string]any{
		"credential_id": credID,
		"content":       map[string]any{"zone": "north", "level": 5},
	})
	if st != http.StatusOK || body["verdict"] != "CONTENT_MISMATCH" {
		t.Fatalf("content: %d %v", st, body)
	}

	// Verify again with the SAME parameters (cache hit) then revoke.
	st, pre := postJSON(t, srv.URL+"/v1/verify", map[string]any{
		"credential_id":    credID,
		"expected_purpose": "door-access",
		"content":          map[string]any{"zone": "north", "level": 4},
	})
	if pre["cache_hit"] != true {
		t.Fatalf("expected cache hit: %v", pre)
	}
	preSnap := int64(pre["snapshot"].(float64))

	// Revoke scheduled to take effect 2 minutes after issuance: the event
	// is recorded now (snapshot moves, cache invalidated) but historical
	// replay decides from as_of whether it applies.
	issuedAt, err := time.Parse(time.RFC3339Nano, issueBody["issued_at"].(string))
	if err != nil {
		t.Fatal(err)
	}
	effective := issuedAt.Add(2 * time.Minute).Format(time.RFC3339Nano)
	st, revBody := postJSON(t, srv.URL+"/v1/credentials/"+credID+"/revoke",
		map[string]any{"reason": "lost badge", "effective_at": effective})
	if st != http.StatusCreated {
		t.Fatalf("revoke: %d %v", st, revBody)
	}
	revSnap := int64(revBody["snapshot"].(float64))
	if revSnap <= preSnap {
		t.Fatalf("revoke snapshot %d <= verify snapshot %d", revSnap, preSnap)
	}

	// "Now" (before the scheduled effective instant): VALID but already at
	// the new snapshot and never served from the pre-revoke cache.
	st, post := postJSON(t, srv.URL+"/v1/verify", map[string]any{"credential_id": credID})
	if st != http.StatusOK {
		t.Fatalf("post verify: %d", st)
	}
	if post["verdict"] != "VALID" {
		t.Fatalf("before scheduled revocation verdict: %v", post)
	}
	if post["cache_hit"] == true {
		t.Fatal("revocation recording must invalidate the cache immediately")
	}
	if int64(post["snapshot"].(float64)) != revSnap {
		t.Fatalf("snapshots differ: revoke=%d verify=%v", revSnap, post["snapshot"])
	}

	// Historical replay 1 minute after issuance (before effective_at):
	// VALID — the current scheduled revocation does not rewrite the past.
	atBefore := issuedAt.Add(time.Minute).Format(time.RFC3339Nano)
	st, hist := postJSON(t, srv.URL+"/v1/verify", map[string]any{
		"credential_id": credID, "as_of": atBefore,
	})
	if st != http.StatusOK || hist["verdict"] != "VALID" {
		t.Fatalf("historical before effective: %d %v", st, hist)
	}
	// Replay 3 minutes after issuance (at/after effective_at): REVOKED.
	atAfter := issuedAt.Add(3 * time.Minute).Format(time.RFC3339Nano)
	st, histAfter := postJSON(t, srv.URL+"/v1/verify", map[string]any{
		"credential_id": credID, "as_of": atAfter,
	})
	if st != http.StatusOK || histAfter["verdict"] != "REVOKED" {
		t.Fatalf("historical after effective: %d %v", st, histAfter)
	}
	if int64(histAfter["snapshot"].(float64)) != revSnap {
		t.Fatalf("historical verify snapshot %v != revoke snapshot %d", histAfter["snapshot"], revSnap)
	}

	// Second revoke -> 409.
	st, dup := postJSON(t, srv.URL+"/v1/credentials/"+credID+"/revoke",
		map[string]any{"reason": "again"})
	if st != http.StatusConflict {
		t.Fatalf("duplicate revoke: %d %v", st, dup)
	}

	// Unknown credential -> UNKNOWN with snapshot.
	st, unk := postJSON(t, srv.URL+"/v1/verify", map[string]any{"credential_id": "cred_nope"})
	if st != http.StatusOK || unk["verdict"] != "UNKNOWN" {
		t.Fatalf("unknown: %d %v", st, unk)
	}
}

func TestHTTPKeyRotation(t *testing.T) {
	srv, _ := newHTTPServer(t)

	_, body := postJSON(t, srv.URL+"/v1/issuers", map[string]any{"name": "krot"})
	issuerID := body["issuer"].(map[string]any)["issuer_id"].(string)
	kid1 := body["genesis_key"].(map[string]any)["kid"].(string)

	st, rot := postJSON(t, srv.URL+"/v1/issuers/"+issuerID+"/keys/rotate", nil)
	if st != http.StatusCreated {
		t.Fatalf("rotate: %d %v", st, rot)
	}
	if rot["previous_key"].(map[string]any)["kid"] != kid1 {
		t.Fatal("rotation did not reference genesis key")
	}
	if rot["previous_key"].(map[string]any)["retired_at"] == nil {
		t.Fatal("previous key not retired")
	}
	kid2 := rot["keypair"].(map[string]any)["kid"].(string)
	if kid2 == kid1 {
		t.Fatal("new key has same kid")
	}

	exp := time.Now().Add(time.Hour).Format(time.RFC3339Nano)
	_, cbody := postJSON(t, srv.URL+"/v1/credentials", map[string]any{
		"issuer_id": issuerID, "subject": "s", "purpose": "p",
		"expires_at": exp, "content": map[string]any{"a": 1},
	})
	if cbody["kid"] != kid2 {
		t.Fatalf("new credential signed by %v, want %s", cbody["kid"], kid2)
	}
}

func TestHTTPValidationErrors(t *testing.T) {
	srv, _ := newHTTPServer(t)

	// Bad JSON.
	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/v1/issuers", bytes.NewReader([]byte("{nope")))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("bad json: %d", resp.StatusCode)
	}

	// Missing name.
	st, body := postJSON(t, srv.URL+"/v1/issuers", map[string]any{})
	if st != http.StatusConflict {
		t.Fatalf("empty name: %d %v", st, body)
	}

	// Revoke unknown credential.
	st, body = postJSON(t, srv.URL+"/v1/revocations", map[string]any{
		"credential_id": "cred_missing", "reason": "x",
	})
	if st != http.StatusNotFound {
		t.Fatalf("revoke missing: %d %v", st, body)
	}

	// Issue from unknown issuer.
	exp := time.Now().Add(time.Hour).Format(time.RFC3339Nano)
	st, body = postJSON(t, srv.URL+"/v1/credentials", map[string]any{
		"issuer_id": "iss_missing", "subject": "s", "purpose": "p",
		"expires_at": exp, "content": map[string]any{},
	})
	if st != http.StatusNotFound {
		t.Fatalf("issue unknown issuer: %d %v", st, body)
	}
}

// TestHTTPConcurrentRevocation runs the race over the real HTTP API:
// exactly one 201, all others 409.
func TestHTTPConcurrentRevocation(t *testing.T) {
	srv, _ := newHTTPServer(t)

	_, body := postJSON(t, srv.URL+"/v1/issuers", map[string]any{"name": "race-ca"})
	issuerID := body["issuer"].(map[string]any)["issuer_id"].(string)
	exp := time.Now().Add(time.Hour).Format(time.RFC3339Nano)
	_, cbody := postJSON(t, srv.URL+"/v1/credentials", map[string]any{
		"issuer_id": issuerID, "subject": "s", "purpose": "p",
		"expires_at": exp, "content": map[string]any{},
	})
	credID := cbody["credential_id"].(string)

	const n = 24
	var wg sync.WaitGroup
	var mu sync.Mutex
	codes := map[int]int{}
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			b, _ := json.Marshal(map[string]any{"reason": "race"})
			req, _ := http.NewRequest(http.MethodPost,
				srv.URL+"/v1/credentials/"+credID+"/revoke", bytes.NewReader(b))
			req.Header.Set("Content-Type", "application/json")
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Error(err)
				return
			}
			resp.Body.Close()
			mu.Lock()
			codes[resp.StatusCode]++
			mu.Unlock()
		}()
	}
	close(start)
	wg.Wait()

	if codes[http.StatusCreated] != 1 {
		t.Fatalf("201 count=%d, want 1 (all codes: %v)", codes[http.StatusCreated], codes)
	}
	if codes[http.StatusConflict] != n-1 {
		t.Fatalf("409 count=%d, want %d (all codes: %v)", codes[http.StatusConflict], n-1, codes)
	}

	// Credential now verifies REVOKED.
	_, vbody := postJSON(t, srv.URL+"/v1/verify", map[string]any{"credential_id": credID})
	if vbody["verdict"] != "REVOKED" {
		t.Fatalf("verdict: %v", vbody)
	}
}

// TestHTTPRetireStopsIssuance: after retire, 422 on issue; rotate resumes.
func TestHTTPRetireStopsIssuance(t *testing.T) {
	srv, _ := newHTTPServer(t)
	_, body := postJSON(t, srv.URL+"/v1/issuers", map[string]any{"name": "ret"})
	issuerID := body["issuer"].(map[string]any)["issuer_id"].(string)

	st, _ := postJSON(t, srv.URL+"/v1/issuers/"+issuerID+"/keys/retire", nil)
	if st != http.StatusOK {
		t.Fatalf("retire: %d", st)
	}
	exp := time.Now().Add(time.Hour).Format(time.RFC3339Nano)
	st, ibody := postJSON(t, srv.URL+"/v1/credentials", map[string]any{
		"issuer_id": issuerID, "subject": "s", "purpose": "p",
		"expires_at": exp, "content": map[string]any{},
	})
	if st != http.StatusUnprocessableEntity {
		t.Fatalf("issue after retire: %d %v", st, ibody)
	}
	st, _ = postJSON(t, srv.URL+"/v1/issuers/"+issuerID+"/keys/rotate", nil)
	if st != http.StatusCreated {
		t.Fatalf("rotate: %d", st)
	}
	st, ibody = postJSON(t, srv.URL+"/v1/credentials", map[string]any{
		"issuer_id": issuerID, "subject": "s", "purpose": "p",
		"expires_at": exp, "content": map[string]any{},
	})
	if st != http.StatusCreated {
		t.Fatalf("issue after rotate: %d %v", st, ibody)
	}
}

// TestSnapshotEndpointAndReplayExample demonstrates the snapshot endpoint
// moves with every append.
func TestSnapshotEndpointAndReplayExample(t *testing.T) {
	srv, _ := newHTTPServer(t)
	snap0 := getSnapshot(t, srv.URL)
	_, body := postJSON(t, srv.URL+"/v1/issuers", map[string]any{"name": "s"})
	_ = body
	snap1 := getSnapshot(t, srv.URL)
	if snap1 <= snap0 {
		t.Fatalf("snapshot did not advance after issuer create: %d -> %d", snap0, snap1)
	}
	if snap1-snap0 != 1 {
		t.Fatalf("issuer create should consume exactly one snapshot: %d -> %d", snap0, snap1)
	}
}

func getSnapshot(t *testing.T, base string) int64 {
	t.Helper()
	resp, err := http.Get(base + "/v1/snapshot")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("snapshot: %d", resp.StatusCode)
	}
	var out struct {
		Snapshot int64 `json:"snapshot"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	return out.Snapshot
}
