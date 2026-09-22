package tests

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"testing"
)

// TestAuditChainConcurrentAppends: parallel append across multiple
// goroutines and clients produces a gapless, fork-free 1..N chain whose
// hashes all verify.
func TestAuditChainConcurrentAppends(t *testing.T) {
	env := newTestEnv(t)
	slug := uniqueSlug("chain")
	tk := env.seedOrg(t, slug, "UTC")
	orgID := env.orgID(t, slug)

	// A rule configure appends one audit entry; hammer the endpoint
	// concurrently (all admin-permitted).
	const writers = 12
	const perWriter = 10
	var wg sync.WaitGroup
	errCh := make(chan error, writers*perWriter)
	for w := 0; w < writers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for i := 0; i < perWriter; i++ {
				body := map[string]any{
					"kind":           "rate",
					"name":           fmt.Sprintf("w%d-i%d", w, i),
					"window_seconds": 300, "max_events": 500,
				}
				b, _ := json.Marshal(body)
				req, _ := http.NewRequest(http.MethodPost,
					env.srv.URL+fmt.Sprintf("/api/v1/orgs/%s/rules", slug),
					bytes.NewReader(b))
				req.Header.Set("Content-Type", "application/json")
				req.Header.Set("Authorization", "Bearer "+tk.Admin)
				resp, err := http.DefaultClient.Do(req)
				if err != nil {
					errCh <- err
					return
				}
				if resp.StatusCode != http.StatusCreated {
					errCh <- fmt.Errorf("writer %d: status %d", w, resp.StatusCode)
				}
				resp.Body.Close()
			}
		}(w)
	}
	wg.Wait()
	close(errCh)
	for err := range errCh {
		t.Fatalf("concurrent append: %v", err)
	}

	want := int64(writers * perWriter)
	if got := auditMaxSeq(t, env, orgID); got != want {
		t.Fatalf("max seq=%d want %d (gap or fork suspected)", got, want)
	}

	// No duplicate seqs, every seq 1..N present.
	var distinct int
	err := env.pool.QueryRow(context.Background(),
		`SELECT count(DISTINCT seq) FROM audit_entries WHERE org_id=$1`, orgID).Scan(&distinct)
	if err != nil {
		t.Fatalf("distinct seq: %v", err)
	}
	if int64(distinct) != want {
		t.Fatalf("distinct seq=%d want %d", distinct, want)
	}

	// Verification endpoint: healthy.
	st, out := env.do(t, http.MethodGet,
		fmt.Sprintf("/api/v1/orgs/%s/audit/verify", slug), tk.Auditor, nil)
	env.mustStatus(t, st, 200, out)
	if out["healthy"] != true {
		t.Fatalf("chain unhealthy after concurrent appends: %v", out["issues"])
	}
}

// TestAuditChainDetectsMissingTamperedOutOfOrder: the verifier reports a
// gap when an entry is removed, a broken link / bad hash for a rewritten
// entry, and flags out-of-order reinsertion. These checks bypass the
// append-only trigger as the table owner.
func TestAuditChainDetectsMissingTamperedOutOfOrder(t *testing.T) {
	env := newTestEnv(t)
	slug := uniqueSlug("verify")
	tk := env.seedOrg(t, slug, "UTC")
	orgID := env.orgID(t, slug)

	// Generate 4 chain entries via rule configurations.
	for i := 0; i < 4; i++ {
		st, out := createRule(t, env, tk.Admin, slug, map[string]any{
			"kind": "rate", "name": fmt.Sprintf("r%d", i),
			"window_seconds": 300, "max_events": 500,
		})
		env.mustStatus(t, st, http.StatusCreated, out)
	}

	verify := func() (int, map[string]any) {
		return env.do(t, http.MethodGet,
			fmt.Sprintf("/api/v1/orgs/%s/audit/verify", slug), tk.Auditor, nil)
	}
	st, out := verify()
	env.mustStatus(t, st, 200, out)

	// --- Detect tampering: rewrite content of seq 2 ---------------------
	bypass := func(sql string, args ...any) {
		t.Helper()
		ctx := context.Background()
		if _, err := env.pool.Exec(ctx,
			"SET session_replication_role = replica"); err != nil {
			t.Fatalf("bypass: %v", err)
		}
		if _, err := env.pool.Exec(ctx, sql, args...); err != nil {
			t.Fatalf("tamper sql: %v", err)
		}
		if _, err := env.pool.Exec(ctx,
			"SET session_replication_role = origin"); err != nil {
			t.Fatalf("reset role: %v", err)
		}
	}

	bypass(`UPDATE audit_entries SET content = '{"tampered":true}'::jsonb
	       WHERE org_id=$1 AND seq=2`, orgID)
	st, out = verify()
	if st != http.StatusConflict {
		t.Fatalf("tampered content: status=%d want 409", st)
	}
	if !hasIssue(out, "tampered_content", 2) {
		t.Fatalf("expected tampered_content issue at seq 2, got %v", out["issues"])
	}
	// Tampering only content (not content_hash) leaves the links intact:
	// exactly one issue, the content mismatch.
	for _, it := range asSlice(out["issues"]) {
		m := asMap(it)
		if m["kind"] != "tampered_content" {
			t.Fatalf("content-only tamper should leave links intact, got %v", out["issues"])
		}
	}

	// Restore content hash consistency is impossible without recompute;
	// instead test deletion -> gap.
	bypass(`DELETE FROM audit_entries WHERE org_id=$1 AND seq=2`, orgID)
	st, out = verify()
	if st != http.StatusConflict {
		t.Fatalf("deleted entry: status=%d want 409", st)
	}
	if !hasIssue(out, "gap", 2) {
		t.Fatalf("expected gap at seq 2 after deletion, got %v", out["issues"])
	}

	// --- Out-of-order / reinsertion: insert a fake seq 2 whose prev_hash
	// points nowhere valid -----------------------------------------------
	// First restore a healthy chain by truncating back to seq 1 (bypassing
	// the trigger), then re-add a bogus entry claiming seq 2.
	bypass(`DELETE FROM audit_entries WHERE org_id=$1 AND seq >= 2`, orgID)
	bypass(`INSERT INTO audit_entries
	        (org_id, seq, actor_id, action, content, content_hash, prev_hash, entry_hash)
	        VALUES ($1, 2, NULL, 'forged', '{"forged":true}'::jsonb,
	                'deadbeef', 'cafef00d', '0badf00d')`, orgID)
	st, out = verify()
	if st != http.StatusConflict {
		t.Fatalf("forged entry: status=%d want 409", st)
	}
	if !hasIssue(out, "broken_link", 2) {
		t.Fatalf("expected broken_link for forged seq 2, got %v", out["issues"])
	}
	if !hasIssue(out, "bad_hash", 2) {
		t.Fatalf("expected bad_hash for forged seq 2, got %v", out["issues"])
	}
}

// TestAppendOnlyTrigger: even an admin cannot UPDATE/DELETE audit rows over
// the normal connection.
func TestAppendOnlyTrigger(t *testing.T) {
	env := newTestEnv(t)
	slug := uniqueSlug("trigger")
	tk := env.seedOrg(t, slug, "UTC")
	orgID := env.orgID(t, slug)
	st, out := createRule(t, env, tk.Admin, slug, map[string]any{
		"kind": "rate", "name": "r", "window_seconds": 300, "max_events": 500,
	})
	env.mustStatus(t, st, http.StatusCreated, out)

	ctx := context.Background()
	if _, err := env.pool.Exec(ctx,
		`UPDATE audit_entries SET action='hax' WHERE org_id=$1 AND seq=1`, orgID); err == nil {
		t.Fatal("UPDATE on audit_entries must be rejected")
	}
	if _, err := env.pool.Exec(ctx,
		`DELETE FROM audit_entries WHERE org_id=$1 AND seq=1`); err == nil {
		t.Fatal("DELETE on audit_entries must be rejected")
	}
}

func hasIssue(out map[string]any, kind string, seq int64) bool {
	for _, it := range asSlice(out["issues"]) {
		m := asMap(it)
		if m["kind"] == kind && int64(intOr(m["seq"])) == seq {
			return true
		}
	}
	return false
}
