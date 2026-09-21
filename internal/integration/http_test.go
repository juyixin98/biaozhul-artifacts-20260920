package integration

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/clearsettle/clearsettle/internal/api"
	"github.com/clearsettle/clearsettle/internal/service"
)

func TestHTTPEndToEnd(t *testing.T) {
	url, cleanup := startPostgres(t)
	defer cleanup()
	e := newTestEnv(t, url)
	handler := api.NewServer(e.svc)

	m, op, _ := e.createMerchantWithKeys(t, "E2E Corp")
	opKey := issueKey(t, e, op)
	audKey := issueKey(t, e, service.Actor{Role: "auditor", MerchantID: &m.ID})
	adminKey := e.adminKey

	do := func(key, method, path, idemKey string, body any) (int, map[string]any) {
		t.Helper()
		var rdr *bytes.Reader
		if body != nil {
			raw, _ := json.Marshal(body)
			rdr = bytes.NewReader(raw)
		} else {
			rdr = bytes.NewReader(nil)
		}
		req := httptest.NewRequest(method, path, rdr)
		req.Header.Set("Authorization", "Bearer "+key)
		if idemKey != "" {
			req.Header.Set("Idempotency-Key", idemKey)
		}
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		var out map[string]any
		if rec.Body.Len() > 0 {
			_ = json.Unmarshal(rec.Body.Bytes(), &out)
		}
		return rec.Code, out
	}

	// No auth -> 403.
	req := httptest.NewRequest(http.MethodGet, "/v1/merchants", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusForbidden, rec.Code)

	// Auditor cannot authorize -> 403.
	code, _ := do(audKey, http.MethodPost, "/v1/payments/authorize", "k",
		map[string]any{"merchant_id": m.ID.String(), "amount_cents": 1000, "card_last4": "1111"})
	assert.Equal(t, http.StatusForbidden, code)

	// Missing idempotency key -> 422.
	code, _ = do(opKey, http.MethodPost, "/v1/payments/authorize", "",
		map[string]any{"merchant_id": m.ID.String(), "amount_cents": 1000, "card_last4": "1111"})
	assert.Equal(t, http.StatusUnprocessableEntity, code)

	// Authorize + capture over the wire; same idem key replays (201 then 200).
	authBody := map[string]any{"merchant_id": m.ID.String(), "amount_cents": 5000, "card_last4": "4242"}
	code, authOut := do(opKey, http.MethodPost, "/v1/payments/authorize", "e2e-auth", authBody)
	require.Equal(t, http.StatusCreated, code)
	paymentID, _ := authOut["id"].(string)
	assert.Equal(t, "•••• •••• •••• 4242", authOut["card"], "card must be masked")

	code, authReplay := do(opKey, http.MethodPost, "/v1/payments/authorize", "e2e-auth", authBody)
	require.Equal(t, http.StatusOK, code)
	assert.Equal(t, paymentID, authReplay["id"])

	code, capOut := do(opKey, http.MethodPost, "/v1/payments/capture", "e2e-cap",
		map[string]any{"payment_id": paymentID})
	require.Equal(t, http.StatusCreated, code)
	assert.Equal(t, "captured", capOut["status"])
	assert.EqualValues(t, 175, capOut["fee_cents"]) // 2.9% of 5000 = 145 + 30

	// Same key different params -> 409.
	code, errOut := do(opKey, http.MethodPost, "/v1/payments/capture", "e2e-cap",
		map[string]any{"payment_id": paymentID, "capture_cents": 4000})
	require.Equal(t, http.StatusConflict, code)
	assert.Contains(t, errOut["error"], "different parameters")

	// Partial refund, then settle via admin.
	code, _ = do(opKey, http.MethodPost, "/v1/payments/refund", "e2e-ref",
		map[string]any{"payment_id": paymentID, "amount_cents": 2000})
	require.Equal(t, http.StatusCreated, code)

	code, batchOut := do(adminKey, http.MethodPost,
		"/v1/merchants/"+m.ID.String()+"/settle?date="+e.clock.Now().Format("2006-01-02"), "", nil)
	require.Equal(t, http.StatusOK, code)
	assert.Equal(t, "done", batchOut["status"])
	assert.EqualValues(t, 3000, batchOut["total_cents"])
	assert.EqualValues(t, 117, batchOut["fee_cents"]) // 175 fee - 58 proportional release
	assert.EqualValues(t, 2883, batchOut["net_cents"])

	// Settle again is idempotent.
	code, again := do(adminKey, http.MethodPost,
		"/v1/merchants/"+m.ID.String()+"/settle?date="+e.clock.Now().Format("2006-01-02"), "", nil)
	require.Equal(t, http.StatusOK, code)
	assert.Equal(t, true, again["already_existed"])

	// Reconcile: sync simulated gateway feed first; clean run.
	code, syncOut := do(adminKey, http.MethodPost, "/v1/admin/statements/sync", "",
		map[string]any{"merchant_id": m.ID.String(), "as_of": e.clock.Now().Format("2006-01-02")})
	require.Equal(t, http.StatusOK, code)
	assert.NotZero(t, syncOut["rows_synced"])

	code, reconOut := do(adminKey, http.MethodPost,
		"/v1/merchants/"+m.ID.String()+"/reconcile?date="+e.clock.Now().Format("2006-01-02"), "", nil)
	require.Equal(t, http.StatusOK, code)
	assert.EqualValues(t, 0, reconOut["discrepancies_count"])

	// Operator cannot settle or administer merchants -> 403.
	other, _ := uuid.NewUUID()
	code, _ = do(opKey, http.MethodPost,
		"/v1/merchants/"+other.String()+"/settle", "", nil)
	assert.Equal(t, http.StatusForbidden, code)
	code, _ = do(opKey, http.MethodPost, "/v1/admin/merchants", "", map[string]any{"name": "x"})
	assert.Equal(t, http.StatusForbidden, code)

	// Audit log is populated and readable by the operator.
	req = httptest.NewRequest(http.MethodGet, "/v1/audit?limit=20", nil)
	req.Header.Set("Authorization", "Bearer "+opKey)
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)
	var events struct {
		Events []map[string]any `json:"events"`
	}
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &events))
	assert.NotEmpty(t, events.Events)

	// Masked key listing never reveals secrets.
	code, keysOut := do(adminKey, http.MethodGet, "/v1/admin/keys", "", nil)
	require.Equal(t, http.StatusOK, code)
	keys := keysOut["keys"].([]any)
	for _, k := range keys {
		km := k.(map[string]any)
		assert.Contains(t, km["prefix"], "•")
		_, hasSecret := km["api_key"]
		assert.False(t, hasSecret)
	}
}

// issueKey mints a fresh key for a merchant role and returns its plaintext.
func issueKey(t *testing.T, e *testEnv, actor service.Actor) string {
	t.Helper()
	issued, err := e.svc.IssueAPIKey(context.Background(), e.admin, service.IssueKeyInput{
		Role: actor.Role, MerchantID: actor.MerchantID,
	})
	require.NoError(t, err)
	return issued.Key
}
