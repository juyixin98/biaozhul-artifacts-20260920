package participant_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"tpc/internal/participant"
)

func newTestNode(t *testing.T) (*participant.State, http.Handler) {
	t.Helper()
	st, err := participant.New("ptest", t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	return st, st.Handler()
}

func post(t *testing.T, h http.Handler, path string, body any) (int, map[string]any) {
	t.Helper()
	buf, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(buf))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	out := map[string]any{}
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec.Code, out
}

func get(t *testing.T, h http.Handler, path string) (int, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, path, nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	out := map[string]any{}
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec.Code, out
}

// Prepared participants must keep waiting: prepare then a long delay (in the
// real system, coordinator failure) leaves the txn PREPARED, locks held, and
// no autonomous abort path exists in the API.
func TestPreparedBlocksWithoutTimeout(t *testing.T) {
	_, h := newTestNode(t)

	code, resp := post(t, h, "/prepare", map[string]any{
		"txid":   "T1",
		"writes": []map[string]string{{"key": "x", "value": "1"}},
	})
	if code != http.StatusOK || resp["vote"] != "yes" {
		t.Fatalf("prepare = %d %v", code, resp)
	}

	// A conflicting transaction cannot get the lock while T1 is prepared.
	code, resp = post(t, h, "/prepare", map[string]any{
		"txid":   "T2",
		"writes": []map[string]string{{"key": "x", "value": "2"}},
	})
	if code != http.StatusConflict {
		t.Fatalf("conflicting prepare = %d %v, want 409", code, resp)
	}

	code, resp = post(t, h, "/recover", map[string]any{})
	if code != http.StatusOK {
		t.Fatalf("recover = %d", code)
	}
	blocked, ok := resp["blocked"].([]any)
	if !ok || len(blocked) != 1 {
		t.Fatalf("blocked = %v, want T1 still blocked", resp["blocked"])
	}

	code, resp = get(t, h, "/kv/x")
	if code != http.StatusOK || resp["value"] != "" || resp["locked"] != true {
		t.Fatalf("kv during block = %d %v, want old value and locked", code, resp)
	}
}

func TestCommitAppliesAndReleasesLock(t *testing.T) {
	_, h := newTestNode(t)
	post(t, h, "/prepare", map[string]any{
		"txid": "T1", "writes": []map[string]string{{"key": "x", "value": "1"}},
	})
	if code, resp := post(t, h, "/commit", map[string]string{"txid": "T1"}); code != http.StatusOK || resp["status"] != "COMMITTED" {
		t.Fatalf("commit = %d %v", code, resp)
	}
	if code, resp := get(t, h, "/kv/x"); code != http.StatusOK || resp["value"] != "1" || resp["locked"] != false {
		t.Fatalf("kv after commit = %d %v", code, resp)
	}
	// Idempotent re-commit.
	if code, _ := post(t, h, "/commit", map[string]string{"txid": "T1"}); code != http.StatusOK {
		t.Fatalf("re-commit = %d, want 200", code)
	}
}

func TestVoteNoOnEmptyKey(t *testing.T) {
	_, h := newTestNode(t)
	if code, resp := post(t, h, "/prepare", map[string]any{
		"txid":   "T1",
		"writes": []map[string]string{{"key": "", "value": "1"}},
	}); code != http.StatusBadRequest || resp["vote"] != "no" {
		t.Fatalf("empty key prepare = %d %v", code, resp)
	}
}

// Abort must be idempotent and release locks; a subsequent commit must fail.
func TestAbortReleasesLock(t *testing.T) {
	_, h := newTestNode(t)
	post(t, h, "/prepare", map[string]any{
		"txid": "T1", "writes": []map[string]string{{"key": "x", "value": "1"}},
	})
	if code, resp := post(t, h, "/abort", map[string]string{"txid": "T1"}); code != http.StatusOK || resp["status"] != "ABORTED" {
		t.Fatalf("abort = %d %v", code, resp)
	}
	if code, _ := post(t, h, "/commit", map[string]string{"txid": "T1"}); code != http.StatusConflict {
		t.Fatalf("commit after abort = %d, want 409", code)
	}
	if code, _ := post(t, h, "/abort", map[string]string{"txid": "T1"}); code != http.StatusOK {
		t.Fatalf("re-abort = %d, want 200", code)
	}
}
