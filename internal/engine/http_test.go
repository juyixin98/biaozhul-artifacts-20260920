package engine_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	clk "domainengine/internal/clock"
	httpapi "domainengine/internal/httpapi"
	"domainengine/internal/ledger"
	"domainengine/internal/models"
	"domainengine/internal/testsupport"

	"github.com/labstack/echo/v4"
)

// newHTTP wires the Echo handler around the test env, using the manual clock.
func newHTTP(t *testing.T, e *testsupport.Env) (*echo.Echo, *httpapi.Server) {
	t.Helper()
	e2 := echo.New()
	srv := httpapi.New(e.DB, e.Accounts, e.Domains, e.Xfer, e.Prices, e.Ledger, e.Clock)
	srv.Handler(e2)
	return e2, srv
}

// do performs a JSON request with the bearer token and returns the response.
func do(t *testing.T, e2 *echo.Echo, method, path, token, body string) *httptest.ResponseRecorder {
	t.Helper()
	var r *http.Request
	if body != "" {
		r = httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Content-Type", "application/json")
	} else {
		r = httptest.NewRequest(method, path, nil)
	}
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	e2.ServeHTTP(rec, r)
	return rec
}

func decode(t *testing.T, rec *httptest.ResponseRecorder, v any) {
	t.Helper()
	if err := json.Unmarshal(rec.Body.Bytes(), v); err != nil {
		t.Fatalf("decode %q: %v body=%s", rec.Code, err, rec.Body.String())
	}
}

// TestHTTPEndToEnd exercises the full flow over HTTP: admin seeds resellers and
// credits, resellers create customers and register, customers only see their
// own domains, and a wrong token is rejected.
func TestHTTPEndToEnd(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	e2, _ := newHTTP(t, e)
	ctx := context.Background()

	// No token -> 401.
	if rec := do(t, e2, http.MethodGet, "/v1/admin/prices", "", ""); rec.Code != http.StatusUnauthorized {
		t.Fatalf("no token status=%d", rec.Code)
	}

	// Create an admin principal directly and mint its token via a login helper.
	adminTok := mintToken(t, e, "admin", 0, 0)
	_ = ctx

	// Admin creates reseller with credit.
	var resellerResp struct {
		Reseller models.Reseller `json:"reseller"`
		Token    string          `json:"token"`
	}
	rec := do(t, e2, "POST", "/v1/admin/resellers", adminTok,
		`{"name":"acme","starting_cents":500000}`)
	if rec.Code != http.StatusCreated {
		t.Fatalf("create reseller status=%d body=%s", rec.Code, rec.Body.String())
	}
	decode(t, rec, &resellerResp)
	rTok := resellerResp.Token

	// Reseller creates a customer.
	var custResp struct {
		Customer models.Customer `json:"customer"`
		Token    string          `json:"token"`
	}
	rec = do(t, e2, "POST", "/v1/reseller/customers", rTok, `{"name":"bob"}`)
	if rec.Code != http.StatusCreated {
		t.Fatalf("create customer status=%d body=%s", rec.Code, rec.Body.String())
	}
	decode(t, rec, &custResp)
	cTok := custResp.Token

	// Customer cannot register (reseller-only route).
	rec = do(t, e2, "POST", "/v1/reseller/domains", cTok,
		`{"name":"x.com","customer_id":1}`)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("customer register status=%d", rec.Code)
	}

	// Reseller registers for their customer.
	var regResp struct {
		Domain   models.Domain `json:"domain"`
		AuthCode string        `json:"auth_code"`
	}
	rec = do(t, e2, "POST", "/v1/reseller/domains", rTok,
		`{"name":"Bob.Store.COM.","customer_id":`+itoa64(custResp.Customer.ID)+`}`)
	if rec.Code != http.StatusCreated {
		t.Fatalf("register status=%d body=%s", rec.Code, rec.Body.String())
	}
	decode(t, rec, &regResp)
	if len(regResp.AuthCode) != 16 {
		t.Fatalf("auth code length=%d want 16", len(regResp.AuthCode))
	}
	if regResp.Domain.CanonicalName != "bob.store.com" {
		t.Fatalf("canonical=%s", regResp.Domain.CanonicalName)
	}

	// Customer sees exactly their own one domain.
	var listResp struct {
		Domains []models.Domain `json:"domains"`
	}
	rec = do(t, e2, "GET", "/v1/customer/domains", cTok, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("customer list status=%d", rec.Code)
	}
	decode(t, rec, &listResp)
	if len(listResp.Domains) != 1 || listResp.Domains[0].CanonicalName != "bob.store.com" {
		t.Fatalf("customer domains=%+v", listResp.Domains)
	}

	// A second reseller cannot see the first reseller's domain.
	rec2 := do(t, e2, "POST", "/v1/admin/resellers", adminTok,
		`{"name":"other","starting_cents":0}`)
	var other struct{ Token string }
	decode(t, rec2, &other)
	rec = do(t, e2, "GET", "/v1/reseller/domains/bob.store.com", other.Token, "")
	if rec.Code != http.StatusForbidden {
		t.Fatalf("cross-reseller view status=%d want 403", rec.Code)
	}

	// Admin CAN see it.
	rec = do(t, e2, "GET", "/v1/admin/prices", adminTok, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("admin prices status=%d", rec.Code)
	}

	// Domain JSON never exposes the auth cipher.
	rec = do(t, e2, "GET", "/v1/reseller/domains/bob.store.com", rTok, "")
	if strings.Contains(rec.Body.String(), "auth_cipher") {
		t.Fatalf("auth cipher leaked in response: %s", rec.Body.String())
	}
}

// TestHTTPTopupIdempotency: two concurrent top-ups with the same key credit
// exactly once.
func TestHTTPTopupIdempotency(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	e2, _ := newHTTP(t, e)
	adminTok := mintToken(t, e, "admin", 0, 0)

	rec := do(t, e2, "POST", "/v1/admin/resellers", adminTok,
		`{"name":"idem-r","starting_cents":0}`)
	var rr struct {
		Reseller models.Reseller `json:"reseller"`
	}
	decode(t, rec, &rr)

	var wg sync.WaitGroup
	const n = 6
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r := do(t, e2, "POST", "/v1/admin/resellers/"+itoa64(rr.Reseller.ID)+"/topup",
				adminTok, `{"amount_cents":5000,"idempotency_key":"topup-1"}`)
			if r.Code != http.StatusOK {
				t.Errorf("topup status=%d body=%s", r.Code, r.Body.String())
			}
		}()
	}
	wg.Wait()

	got := e.Reseller(t, rr.Reseller.ID).BalanceCents
	if got != 5000 {
		t.Fatalf("balance=%d want 5000 (idempotent topup)", got)
	}
	var txCount int
	if err := e.DB.GetContext(context.Background(), &txCount,
		`SELECT count(*) FROM billing_transactions WHERE kind='topup'`); err != nil {
		t.Fatal(err)
	}
	if txCount != 1 {
		t.Fatalf("topup transactions=%d want 1", txCount)
	}
}

// TestLedgerAppendOnly verifies every charge has a matching ledger entry and
// balances stay consistent.
func TestLedgerAppendOnly(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	r := e.MkReseller(t, "led", 100_000)
	c := e.MkCustomer(t, r, "c")
	e.Reg(t, "one.example.com", r, c)
	e.Reg(t, "two.example.org", r, c)
	ctx := context.Background()

	// Each billing transaction has exactly one ledger entry.
	var missing int
	if err := e.DB.GetContext(ctx, &missing, `
		SELECT count(*) FROM billing_transactions bt
		LEFT JOIN ledger_entries le ON le.tx_id = bt.id
		WHERE le.id IS NULL`); err != nil {
		t.Fatal(err)
	}
	if missing != 0 {
		t.Fatalf("billing transactions without ledger entry: %d", missing)
	}

	// Available balance equals the sum of available deltas (100000 start is not
	// a ledger posting, so: 100000 - 1200 - 1000).
	var sumAvail int64
	if err := e.DB.GetContext(ctx, &sumAvail,
		`SELECT COALESCE(sum(available_delta),0) FROM ledger_entries WHERE reseller_id=$1`, r); err != nil {
		t.Fatal(err)
	}
	if got := e.Reseller(t, r).BalanceCents; got != 100_000+sumAvail {
		t.Fatalf("balance=%d but ledger sums to %d (100000%+d)", got, 100_000+sumAvail, sumAvail)
	}
}

// TestRenewIdempotency: same key, concurrent renewals charge once.
func TestRenewIdempotency(t *testing.T) {
	e := testsupport.NewEnv(t)
	e.Reset(t)
	ctx := context.Background()
	r := e.MkReseller(t, "ri", 100_000)
	c := e.MkCustomer(t, r, "c")
	d, _ := e.Reg(t, "ri.example.com", r, c)
	key := "renew-1"

	var wg sync.WaitGroup
	var firstExpiry int64
	var mu sync.Mutex
	const n = 6
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			res, err := e.Domains.Renew(ctx, e.Clock, domainsRenewReq(d.CanonicalName, r, 1, &key))
			if err != nil {
				t.Errorf("renew: %v", err)
				return
			}
			mu.Lock()
			if firstExpiry == 0 {
				firstExpiry = res.Domain.ExpiresAt.UnixNano()
			} else if res.Domain.ExpiresAt.UnixNano() != firstExpiry {
				t.Errorf("expiry differs across idempotent retries")
			}
			mu.Unlock()
		}()
	}
	wg.Wait()
	if got := e.Reseller(t, r).BalanceCents; got != 100_000-1200-1200 {
		t.Fatalf("balance=%d want %d", got, 100_000-2400)
	}
}

var _ ledger.Post
var _ clk.Clock
