package tests

import (
	"context"
	"fmt"
	"net/http"
	"sync"
	"testing"

	"dams/internal/platform/canonical"
)

// 30 concurrent rule-version creations must produce a gapless, fork-free
// audit chain with sequence numbers 1..30 even under contention.
func TestAuditChainConcurrentAppend(t *testing.T) {
	h := Setup(t, "UTC")

	const n = 30
	var wg sync.WaitGroup
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, err := h.Deps.Rules.Create(context.Background(), h.Org.ID, actor("admin"),
				createFreqRule(i))
			errs <- err
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("concurrent rule create: %v", err)
		}
	}

	body := h.MustStatus(http.MethodGet, "/v1/audit/verify", h.Keys.Auditor, http.StatusOK, nil)
	if body["ok"] != true {
		t.Fatalf("chain not OK after concurrent appends: %v", body["issues"])
	}
	if body["head_seq"].(float64) != n {
		t.Fatalf("head_seq = %v, want %d", body["head_seq"], n)
	}

	// Entries must be exactly seq 1..n with unique, gap-free numbers.
	list := h.MustStatus(http.MethodGet,
		fmt.Sprintf("/v1/audit/entries?limit=%d", n), h.Keys.Auditor, http.StatusOK, nil)
	entries := list["entries"].([]any)
	if len(entries) != n {
		t.Fatalf("entries = %d, want %d", len(entries), n)
	}
	seen := map[int64]bool{}
	for i, e := range entries {
		m := e.(map[string]any)
		seq := int64(m["seq"].(float64))
		if seq != int64(i+1) {
			t.Fatalf("entry %d has seq=%d, gap or fork", i, seq)
		}
		if seen[seq] {
			t.Fatalf("duplicate seq %d (fork)", seq)
		}
		seen[seq] = true
		if m["entry_type"] != "rule.create" {
			t.Fatalf("entry %d type = %v", i, m["entry_type"])
		}
	}
}

// Verify detects tampering (payload modified) and missing entries (gap).
func TestAuditChainVerifyDetectsTamperAndGap(t *testing.T) {
	h := Setup(t, "UTC")
	for i := 0; i < 10; i++ {
		h.MustStatus(http.MethodPost, "/v1/rules/", h.Keys.Admin, http.StatusCreated, map[string]any{
			"rule_type": "frequency",
			"params": map[string]any{
				"window_seconds": 300, "threshold": 500 + i,
			},
		})
	}

	// Tamper with seq=5 and delete seq=6 directly in the database, bypassing
	// the append API (simulates storage-level manipulation).
	tampered := map[string]any{"forged": true}
	raw, _ := canonical.JSON(tampered)
	if _, err := h.Pool.Exec(context.Background(),
		`UPDATE audit_entries SET payload = $1 WHERE org_id = $2 AND seq = 5`,
		string(raw), h.Org.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := h.Pool.Exec(context.Background(),
		`DELETE FROM audit_entries WHERE org_id = $1 AND seq = 6`, h.Org.ID); err != nil {
		t.Fatal(err)
	}

	rec, body := h.Do(http.MethodGet, "/v1/audit/verify", h.Keys.Auditor, nil)
	if rec.StatusCode != http.StatusConflict {
		t.Fatalf("verify status = %d, want 409", rec.StatusCode)
	}
	if body["ok"] != false {
		t.Fatalf("verify ok = true, want false")
	}
	issues := body["issues"].([]any)
	kinds := map[string]bool{}
	for _, is := range issues {
		kinds[is.(map[string]any)["kind"].(string)] = true
	}
	if !kinds["tampered"] {
		t.Fatalf("tampering not detected; issues=%v", issues)
	}
	if !kinds["gap"] {
		t.Fatalf("missing entry not detected; issues=%v", issues)
	}
}

// Out-of-order writes are impossible through the API; but verify also flags a
// forked head (entry seq beyond stored head). The advisory lock prevents this
// in normal operation — we simulate by rewinding only the head pointer.
func TestAuditChainDetectsHeadMismatch(t *testing.T) {
	h := Setup(t, "UTC")
	for i := 0; i < 3; i++ {
		h.MustStatus(http.MethodPost, "/v1/rules/", h.Keys.Admin, http.StatusCreated, map[string]any{
			"rule_type": "frequency",
			"params":    map[string]any{"window_seconds": 300, "threshold": 500},
		})
	}
	if _, err := h.Pool.Exec(context.Background(),
		`UPDATE audit_chain_state SET head_seq = 2 WHERE org_id = $1`, h.Org.ID); err != nil {
		t.Fatal(err)
	}
	rec, body := h.Do(http.MethodGet, "/v1/audit/verify", h.Keys.Auditor, nil)
	if rec.StatusCode != http.StatusConflict {
		t.Fatalf("status = %d, want 409", rec.StatusCode)
	}
	kinds := map[string]bool{}
	for _, is := range body["issues"].([]any) {
		kinds[is.(map[string]any)["kind"].(string)] = true
	}
	if !kinds["head_mismatch"] {
		t.Fatalf("head mismatch not detected: %v", body["issues"])
	}
}
