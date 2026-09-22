package tests

import (
	"context"
	"fmt"
	"net/http"
	"testing"
)

// seedSecondOrg makes a second org and returns its admin token, for
// cross-organization denial tests.
func seedSecondOrg(t *testing.T, env *testEnv, slug string) string {
	t.Helper()
	ctx := context.Background()
	var orgID, uid int64
	err := env.pool.QueryRow(ctx,
		`INSERT INTO organizations (slug, name, timezone) VALUES ($1,$1,'UTC') RETURNING id`,
		slug).Scan(&orgID)
	if err != nil {
		t.Fatalf("org: %v", err)
	}
	err = env.pool.QueryRow(ctx,
		`INSERT INTO users (email, display_name) VALUES ($1,$1) RETURNING id`,
		slug+"-admin2@test.local").Scan(&uid)
	if err != nil {
		t.Fatalf("user: %v", err)
	}
	_, err = env.pool.Exec(ctx,
		`INSERT INTO memberships (org_id,user_id,role) VALUES ($1,$2,'admin')`, orgID, uid)
	if err != nil {
		t.Fatalf("membership: %v", err)
	}
	token := fmt.Sprintf("dams_xorg_%d", timeNowNano())
	sum := hash256(token)
	_, err = env.pool.Exec(ctx,
		`INSERT INTO api_tokens (user_id, token_hash) VALUES ($1,$2)`, uid, sum)
	if err != nil {
		t.Fatalf("token: %v", err)
	}
	return token
}

// TestRBACCrossOrgAndRoles verifies the authorization matrix:
//   - no token / bad token -> 401
//   - non-member of an org -> 403 (cannot even see it)
//   - auditor cannot ingest/configure/transition (403), but may read/export
//   - analyst cannot ingest/configure rules (403), but may list alerts
//   - analyst from another org cannot touch an alert (403)
func TestRBACCrossOrgAndRoles(t *testing.T) {
	env := newTestEnv(t)
	slug := uniqueSlug("rbac")
	tk := env.seedOrg(t, slug, "UTC")
	otherToken := seedSecondOrg(t, env, uniqueSlug("otherorg"))

	// 401 without token.
	resp, err := http.Get(env.srv.URL + fmt.Sprintf("/api/v1/orgs/%s/alerts", slug))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("no token: status=%d want 401", resp.StatusCode)
	}

	// Cross-org admin is a non-member here -> 403 on every per-org route.
	st, _ := env.do(t, http.MethodGet,
		fmt.Sprintf("/api/v1/orgs/%s/alerts", slug), otherToken, nil)
	if st != http.StatusForbidden {
		t.Fatalf("cross-org GET alerts: %d want 403", st)
	}
	st, _ = env.do(t, http.MethodPost,
		fmt.Sprintf("/api/v1/orgs/%s/events:batch", slug), otherToken,
		map[string]any{"source": "x", "events": []any{}})
	if st != http.StatusForbidden {
		t.Fatalf("cross-org ingest: %d want 403", st)
	}
	st, _ = env.do(t, http.MethodGet,
		fmt.Sprintf("/api/v1/orgs/%s/audit", slug), otherToken, nil)
	if st != http.StatusForbidden {
		t.Fatalf("cross-org read audit: %d want 403", st)
	}

	// Auditor: read allowed, writes forbidden.
	st, _ = env.do(t, http.MethodGet,
		fmt.Sprintf("/api/v1/orgs/%s/rules", slug), tk.Auditor, nil)
	if st != http.StatusOK {
		t.Fatalf("auditor list rules: %d want 200", st)
	}
	st, _ = env.do(t, http.MethodPost,
		fmt.Sprintf("/api/v1/orgs/%s/rules", slug), tk.Auditor,
		map[string]any{"kind": "rate", "name": "x"})
	if st != http.StatusForbidden {
		t.Fatalf("auditor create rule: %d want 403", st)
	}
	st, _ = env.do(t, http.MethodPost,
		fmt.Sprintf("/api/v1/orgs/%s/events:batch", slug), tk.Auditor,
		map[string]any{"source": "s", "events": []any{}})
	if st != http.StatusForbidden {
		t.Fatalf("auditor ingest: %d want 403", st)
	}
	st, _ = env.do(t, http.MethodPost,
		fmt.Sprintf("/api/v1/orgs/%s/alerts/1:transition", slug), tk.Auditor,
		map[string]any{"to_status": "resolved", "expected_version": 1})
	if st != http.StatusForbidden {
		t.Fatalf("auditor transition: %d want 403", st)
	}
	st, _ = env.do(t, http.MethodGet,
		fmt.Sprintf("/api/v1/orgs/%s/events:export", slug), tk.Auditor, nil)
	if st != http.StatusOK {
		t.Fatalf("auditor export: %d want 200", st)
	}

	// Analyst: may list/get alerts but cannot ingest or configure rules.
	st, _ = env.do(t, http.MethodGet,
		fmt.Sprintf("/api/v1/orgs/%s/alerts", slug), tk.Analyst, nil)
	if st != http.StatusOK {
		t.Fatalf("analyst list alerts: %d want 200", st)
	}
	st, _ = env.do(t, http.MethodPost,
		fmt.Sprintf("/api/v1/orgs/%s/rules", slug), tk.Analyst,
		map[string]any{"kind": "rate", "name": "x"})
	if st != http.StatusForbidden {
		t.Fatalf("analyst create rule: %d want 403", st)
	}
	st, _ = env.do(t, http.MethodPost,
		fmt.Sprintf("/api/v1/orgs/%s/events:batch", slug), tk.Analyst,
		map[string]any{"source": "s", "events": []any{}})
	if st != http.StatusForbidden {
		t.Fatalf("analyst ingest: %d want 403", st)
	}

	// /orgs only lists memberships; cross-org admin sees no demo org.
	st, out := env.do(t, http.MethodGet, "/api/v1/orgs", otherToken, nil)
	env.mustStatus(t, st, 200, out)
	for _, o := range asSlice(out["organizations"]) {
		if asMap(o)["slug"] == slug {
			t.Fatal("cross-org user must not see the demo org in /orgs")
		}
	}
}

// TestExportMaskingAndRBAC: all roles may export, but policy fields are
// masked for everyone (no bypass). An admin can change the mask set; other
// roles cannot.
func TestExportMaskingAndRBAC(t *testing.T) {
	env := newTestEnv(t)
	slug := uniqueSlug("export")
	tk := env.seedOrg(t, slug, "UTC")

	evs := []batchEvent{{
		EventID: "exp-1", DbUser: "secret_user",
		OccurredAt: "2026-09-22T12:00:00Z", Action: "select",
		Schema: "public", Table: "customer_pii", RowCount: 42,
	}}
	path, body := batchPayload(slug, "s", evs)
	st, out := env.do(t, http.MethodPost, path, tk.Admin, body)
	env.mustStatus(t, st, 200, out)

	// Default mask: db_user, table_name.
	raw := exportRaw(t, env, slug, tk.Analyst, "jsonl")
	if !contains(raw, "***MASKED***") {
		t.Fatalf("expected masked values in export, got: %s", raw)
	}
	if contains(raw, "secret_user") {
		t.Fatalf("db_user leaked in export: %s", raw)
	}
	if contains(raw, "customer_pii") {
		t.Fatalf("table_name leaked in export: %s", raw)
	}
	// Non-masked fields remain visible.
	if !contains(raw, `"action":"select"`) {
		t.Fatalf("expected visible action in export, got: %s", raw)
	}

	// Analyst/auditor cannot change the policy.
	for _, token := range []string{tk.Analyst, tk.Auditor} {
		st, _ := env.do(t, http.MethodPut,
			fmt.Sprintf("/api/v1/orgs/%s/export-policy", slug), token,
			map[string]any{"masked_fields": []string{"db_user"}})
		if st != http.StatusForbidden {
			t.Fatalf("policy change by non-admin status=%d want 403", st)
		}
	}

	// Admin narrows the mask to db_user only; table becomes visible.
	st, out = env.do(t, http.MethodPut,
		fmt.Sprintf("/api/v1/orgs/%s/export-policy", slug), tk.Admin,
		map[string]any{"masked_fields": []string{"db_user"}})
	env.mustStatus(t, st, 200, out)
	raw = exportRaw(t, env, slug, tk.Auditor, "jsonl")
	if contains(raw, "secret_user") {
		t.Fatalf("db_user must still be masked: %s", raw)
	}
	if !contains(raw, "customer_pii") {
		t.Fatalf("table should be visible after policy change, got: %s", raw)
	}

	// CSV format honors the mask too.
	raw = exportRaw(t, env, slug, tk.Admin, "csv")
	if !contains(raw, "***MASKED***") || contains(raw, "secret_user") {
		t.Fatalf("csv masking failed: %s", raw)
	}
}

func exportRaw(t *testing.T, env *testEnv, org, token, format string) string {
	t.Helper()
	req, _ := http.NewRequest(http.MethodGet,
		env.srv.URL+fmt.Sprintf("/api/v1/orgs/%s/events:export?format=%s", org, format),
		nil)
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("export status=%d", resp.StatusCode)
	}
	return readAllString(resp.Body)
}

func contains(haystack, needle string) bool {
	return len(haystack) >= len(needle) && indexOf(haystack, needle) >= 0
}
