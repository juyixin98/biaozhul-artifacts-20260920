package api_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"revcred/internal/api"
	"revcred/internal/core"
	"revcred/internal/store"
	"revcred/migrations"
)

// 端到端 HTTP 测试：真实数据库 + httptest 服务器。
func newTestServer(t *testing.T) *httptest.Server {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		dsn = os.Getenv("DATABASE_URL")
	}
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set; skipping integration test")
	}
	ctx := context.Background()
	st, err := store.New(ctx, dsn)
	if err != nil {
		t.Fatalf("store.New: %v", err)
	}
	if err := st.Migrate(ctx, migrations.FS); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	if _, err := st.Pool().Exec(ctx,
		"TRUNCATE revocation_events, credentials, issuer_keys RESTART IDENTITY"); err != nil {
		t.Fatalf("truncate: %v", err)
	}
	srv := httptest.NewServer(api.NewRouter(core.NewService(st)))
	t.Cleanup(func() {
		srv.Close()
		st.Close()
	})
	return srv
}

func post(t *testing.T, url string, body any, wantStatus int) map[string]any {
	t.Helper()
	b, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	resp, err := http.Post(url, "application/json", bytes.NewReader(b))
	if err != nil {
		t.Fatalf("POST %s: %v", url, err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.StatusCode != wantStatus {
		t.Fatalf("POST %s: want %d, got %d: %v", url, wantStatus, resp.StatusCode, out)
	}
	return out
}

func TestHTTPFlow(t *testing.T) {
	srv := newTestServer(t)
	base := time.Now().UTC().Add(-time.Hour).Truncate(time.Second)

	// 1. 创建密钥。
	key := post(t, srv.URL+"/v1/keys", map[string]any{
		"issuer":     "issuer-http-1",
		"valid_from": base.Format(time.RFC3339),
	}, http.StatusCreated)
	if key["kid"] == "" || key["public_key"] == "" {
		t.Fatalf("bad key response: %v", key)
	}

	// 2. 发行凭证。
	issue := post(t, srv.URL+"/v1/credentials", map[string]any{
		"issuer":     "issuer-http-1",
		"subject":    "subject-http-1",
		"purpose":    "age-check",
		"not_before": base.Format(time.RFC3339),
		"not_after":  base.Add(48 * time.Hour).Format(time.RFC3339),
		"content":    "hello synthetic world",
	}, http.StatusCreated)
	cred := issue["credential"].(map[string]any)
	credID := cred["id"].(string)
	if cred["signature"] == "" || cred["content_digest"] == "" {
		t.Fatalf("bad credential: %v", cred)
	}

	// 3. 验证：有效。
	v1 := post(t, srv.URL+"/v1/verifications", map[string]any{
		"credential_id": credID,
		"purpose":       "age-check",
	}, http.StatusOK)
	if v1["status"] != "valid" {
		t.Fatalf("want valid, got %v", v1)
	}

	// 4. 用途不匹配。
	v2 := post(t, srv.URL+"/v1/verifications", map[string]any{
		"credential_id": credID,
		"purpose":       "loan-application",
	}, http.StatusOK)
	if v2["status"] != "invalid" {
		t.Fatalf("want invalid for wrong purpose, got %v", v2)
	}

	// 5. 撤销，返回快照号。
	rev := post(t, srv.URL+"/v1/credentials/"+credID+"/revocations",
		map[string]any{"reason": "compromised"}, http.StatusCreated)
	snap := rev["snapshot"].(float64)
	if snap < 1 {
		t.Fatalf("bad snapshot: %v", rev)
	}

	// 6. 撤销后验证：revoked，且快照号一致。
	v3 := post(t, srv.URL+"/v1/verifications", map[string]any{
		"credential_id": credID,
		"purpose":       "age-check",
	}, http.StatusOK)
	if v3["status"] != "invalid" || v3["snapshot"].(float64) != snap {
		t.Fatalf("after revoke: %v (want invalid@%v)", v3, snap)
	}
	reasons := fmt.Sprint(v3["reasons"])
	if !bytes.Contains([]byte(reasons), []byte("revoked")) {
		t.Fatalf("want revoked reason, got %v", v3["reasons"])
	}

	// 7. 历史验证：撤销前的时刻仍有效。
	past := time.Now().UTC().Add(-time.Minute).Format(time.RFC3339)
	v4 := post(t, srv.URL+"/v1/verifications", map[string]any{
		"credential_id": credID,
		"purpose":       "age-check",
		"at":            past,
	}, http.StatusOK)
	if v4["status"] != "valid" {
		t.Fatalf("historical verify: want valid, got %v", v4)
	}

	// 8. 健康检查。
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatalf("healthz: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("healthz: want 200, got %d", resp.StatusCode)
	}
}
