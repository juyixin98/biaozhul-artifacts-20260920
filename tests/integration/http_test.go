package integration

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/clearsettle/clearsettle/internal/admin"
	"github.com/clearsettle/clearsettle/internal/api"
	"github.com/clearsettle/clearsettle/internal/auth"
	"github.com/clearsettle/clearsettle/internal/payments"
	"github.com/clearsettle/clearsettle/internal/recon"
	"github.com/clearsettle/clearsettle/internal/settle"
	"github.com/clearsettle/clearsettle/internal/store"
)

type apiHarness struct{ h http.Handler }

func newAPIHarness(t *testing.T, ctx context.Context, e *env) apiHarness {
	t.Helper()
	return apiHarness{h: api.NewRouter(api.Deps{
		Pool: e.pool, Q: store.New(e.pool), JWTSecret: "test-secret",
		SettleHorizon: 24 * time.Hour,
		Admin:         e.admin, Payments: e.payments, Settle: e.settle, Recon: e.recon,
	})}
}

func (a apiHarness) do(method, path string, hdr map[string]string, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	rr := httptest.NewRecorder()
	a.h.ServeHTTP(rr, req)
	return rr
}

func bearerH(tok string) map[string]string {
	return map[string]string{"Authorization": "Bearer " + tok}
}
func jsonH() map[string]string         { return map[string]string{"Content-Type": "application/json"} }
func idemH(k string) map[string]string { return map[string]string{"Idempotency-Key": k} }

func mergeH(parts ...map[string]string) map[string]string {
	out := map[string]string{}
	for _, m := range parts {
		for k, v := range m {
			out[k] = v
		}
	}
	return out
}

// tokenForMerchant creates a real operator user (so audit FKs hold) and returns
// a token scoped to that merchant.
func tokenForMerchant(t *testing.T, ctx context.Context, e *env, mid uuid.UUID) string {
	t.Helper()
	u, err := e.admin.CreateUser(ctx, admin.CreateUserInput{
		Email:      "op-" + uuid.NewString() + "@test.local",
		Password:   "operator-password",
		Role:       auth.RoleOperator,
		MerchantID: &mid,
	}, admin.Actor{Role: "admin"})
	if err != nil {
		t.Fatal(err)
	}
	tok, err := auth.IssueUserToken("test-secret", auth.Claims{
		Sub: u.ID.String(), Role: auth.RoleOperator, MerchantID: mid.String(),
	})
	if err != nil {
		t.Fatal(err)
	}
	return tok
}

type merchantWithKey struct {
	id  uuid.UUID
	key string
}

func createMerchantWithKey(t *testing.T, ctx context.Context, e *env) merchantWithKey {
	t.Helper()
	res, err := e.admin.CreateMerchant(ctx, admin.CreateMerchantInput{
		Name: "http-" + t.Name() + "-" + uuid.NewString()[:8],
	}, admin.Actor{Role: "admin"})
	if err != nil {
		t.Fatal(err)
	}
	return merchantWithKey{id: res.Merchant.ID, key: res.APIKey}
}

// oldCapture authorizes+captures a payment then backdates it onto oldDay.
func oldCapture(t *testing.T, ctx context.Context, e *env, mid uuid.UUID, keyPrefix string, oldDay time.Time) uuid.UUID {
	t.Helper()
	p, _, err := e.payments.Authorize(ctx, payments.AuthorizeInput{
		MerchantID: mid, IdempotencyKey: keyPrefix + "-auth", Amount: 5000,
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	capP, _, err := e.payments.Capture(ctx, payments.CaptureInput{
		MerchantID: mid, PaymentID: p.ID, IdempotencyKey: keyPrefix + "-cap",
	}, payActor())
	if err != nil {
		t.Fatal(err)
	}
	ts := oldDay.Truncate(24 * time.Hour).Add(12 * time.Hour)
	if _, err := e.pool.Exec(ctx,
		`UPDATE payments SET captured_at=$2, created_at=$2 WHERE id=$1`, capP.ID, ts); err != nil {
		t.Fatal(err)
	}
	if _, err := e.pool.Exec(ctx,
		`UPDATE channel_events SET event_date=$2 WHERE payment_id=$1`, capP.ID, ts); err != nil {
		t.Fatal(err)
	}
	return capP.ID
}

// TestHTTPRoleEnforcement verifies admin/operator/auditor boundaries via real routes.
func TestHTTPRoleEnforcement(t *testing.T) {
	ctx, e := setup(t)
	ts := newAPIHarness(t, ctx, e)

	if rr := ts.do("GET", "/v1/payments", nil, ""); rr.Code != http.StatusUnauthorized {
		t.Fatalf("no-auth GET payments=%d want 401", rr.Code)
	}
	adminTok := mustUserToken(t, auth.RoleAdmin, uuid.Nil)
	audTok := mustUserToken(t, auth.RoleAuditor, uuid.New())
	opTok := mustUserToken(t, auth.RoleOperator, uuid.New())

	if rr := ts.do("GET", "/v1/admin/merchants", bearerH(adminTok), ""); rr.Code != http.StatusOK {
		t.Fatalf("admin list merchants=%d want 200", rr.Code)
	}
	if rr := ts.do("GET", "/v1/admin/merchants", bearerH(opTok), ""); rr.Code != http.StatusForbidden {
		t.Fatalf("operator list merchants=%d want 403", rr.Code)
	}
	if rr := ts.do("POST", "/v1/payments/authorize",
		mergeH(bearerH(audTok), jsonH(), idemH("x")), `{"amount":100}`); rr.Code != http.StatusForbidden {
		t.Fatalf("auditor authorize=%d want 403", rr.Code)
	}
	if rr := ts.do("POST", "/v1/jobs/settle",
		mergeH(bearerH(audTok), jsonH()), `{}`); rr.Code != http.StatusForbidden {
		t.Fatalf("auditor settle=%d want 403", rr.Code)
	}
	if rr := ts.do("GET", "/v1/audit-logs", bearerH(audTok), ""); rr.Code != http.StatusOK {
		t.Fatalf("auditor audit=%d want 200", rr.Code)
	}
	if rr := ts.do("GET", "/v1/settlements", bearerH(audTok), ""); rr.Code != http.StatusOK {
		t.Fatalf("auditor settlements=%d want 200", rr.Code)
	}
}

// TestHTTPCrossMerchantIsolation asserts B's key cannot read A's payment and
// that an operator's settle sweep touches only its own merchant.
func TestHTTPCrossMerchantIsolation(t *testing.T) {
	ctx, e := setup(t)
	ts := newAPIHarness(t, ctx, e)

	a := createMerchantWithKey(t, ctx, e)
	b := createMerchantWithKey(t, ctx, e)

	rr := ts.do("POST", "/v1/payments/authorize",
		mergeH(bearerH(a.key), jsonH(), idemH("iso-auth")), `{"amount":777}`)
	if rr.Code != http.StatusCreated {
		t.Fatalf("authorize=%d body=%s", rr.Code, rr.Body.String())
	}
	pid := extractID(t, rr.Body.String())

	if rr := ts.do("GET", "/v1/payments/"+pid, bearerH(b.key), ""); rr.Code != http.StatusNotFound {
		t.Fatalf("cross-merchant read=%d want 404", rr.Code)
	}
	if rr := ts.do("GET", "/v1/payments/"+pid, bearerH(a.key), ""); rr.Code != http.StatusOK {
		t.Fatalf("own-merchant read=%d want 200", rr.Code)
	}

	oldDay := time.Now().UTC().Add(-72 * time.Hour)
	oldCapture(t, ctx, e, a.id, "iso-a", oldDay)
	oldCapture(t, ctx, e, b.id, "iso-b", oldDay)

	opTok := tokenForMerchant(t, ctx, e, a.id)
	rr = ts.do("POST", "/v1/jobs/settle", mergeH(bearerH(opTok), jsonH()), `{}`)
	if rr.Code != http.StatusOK {
		t.Fatalf("sweep=%d body=%s", rr.Code, rr.Body.String())
	}
	if strings.Contains(rr.Body.String(), b.id.String()) {
		t.Fatalf("operator sweep touched another merchant: %s", rr.Body.String())
	}
	if !strings.Contains(rr.Body.String(), a.id.String()) {
		t.Fatalf("operator sweep did not settle own merchant: %s", rr.Body.String())
	}
	var bSettled bool
	if err := e.pool.QueryRow(ctx,
		`SELECT settlement_batch_id IS NOT NULL FROM payments WHERE merchant_id=$1 ORDER BY id LIMIT 1`,
		b.id).Scan(&bSettled); err != nil {
		t.Fatal(err)
	}
	if bSettled {
		t.Fatal("merchant B payment was settled by merchant A's scoped sweep")
	}
}

func mustUserToken(t *testing.T, role string, mid uuid.UUID) string {
	t.Helper()
	c := auth.Claims{Sub: uuid.NewString(), Role: role}
	if mid != uuid.Nil {
		c.MerchantID = mid.String()
	}
	tok, err := auth.IssueUserToken("test-secret", c)
	if err != nil {
		t.Fatal(err)
	}
	return tok
}

func extractID(t *testing.T, body string) string {
	t.Helper()
	const marker = `"id":"`
	i := strings.Index(body, marker)
	if i < 0 {
		t.Fatalf("no id in body: %s", body)
	}
	rest := body[i+len(marker):]
	j := strings.IndexByte(rest, '"')
	if j < 0 {
		t.Fatalf("unterminated id: %s", body)
	}
	return rest[:j]
}

var _ = settle.Cutoff
var _ = recon.DiscrepancyThreshold
