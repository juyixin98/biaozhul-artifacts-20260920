package tests

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"sync"
	"testing"
	"time"

	"dams/internal/api"
	"dams/internal/db"
	"dams/internal/platform/auth"
	"dams/internal/platform/dbpool"
	"dams/internal/platform/migrate"
	"dams/internal/service/alerts"
	"dams/internal/service/auditchain"
	"dams/internal/service/detection"
	"dams/internal/service/ingest"
	"dams/internal/service/ruleadmin"

	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
)

func testDSN() string {
	if v := os.Getenv("TEST_DATABASE_URL"); v != "" {
		return v
	}
	return "postgres://dams:dams@localhost:55080/postgres?sslmode=disable"
}

var (
	migrateOnce sync.Once
	migrateErr  error
)

type Keys struct {
	Admin     string
	Analyst   string
	Auditor   string
	Collector string
}

type Harness struct {
	T      *testing.T
	Pool   *pgxpool.Pool
	Org    db.Organization
	Keys   Keys
	Router http.Handler
	Deps   api.Deps
}

func newPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	dsn := testDSN()
	pool, err := dbpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(pool.Close)
	return pool
}

// Setup creates an isolated organization with one key per role. Migrations
// run once against the shared test database.
func Setup(t *testing.T, timezone string) *Harness {
	t.Helper()
	pool := newPool(t)
	migrateOnce.Do(func() {
		_, migrateErr = migrate.Up(context.Background(), pool)
	})
	if migrateErr != nil {
		t.Fatalf("migrate: %v", migrateErr)
	}

	ctx := context.Background()
	suffix := randHex(4)
	org, err := db.New(pool).CreateOrganization(ctx, db.CreateOrganizationParams{
		Name:     "test-" + suffix,
		Timezone: timezone,
	})
	if err != nil {
		t.Fatalf("create org: %v", err)
	}

	mkKey := func(role string) string {
		raw := "dams_test_" + role + "_" + randHex(12)
		if _, err := db.New(pool).CreateAPIKey(ctx, db.CreateAPIKeyParams{
			KeyHash: auth.HashKey(raw), KeyPrefix: raw[:16],
			OrgID: org.ID, Name: "test-" + role, Role: role,
		}); err != nil {
			t.Fatalf("create key %s: %v", role, err)
		}
		return raw
	}
	keys := Keys{
		Admin:     mkKey("admin"),
		Analyst:   mkKey("analyst"),
		Auditor:   mkKey("auditor"),
		Collector: mkKey("collector"),
	}

	engine := detection.New()
	chain := auditchain.New()
	deps := api.Deps{
		Pool:   pool,
		Ingest: ingest.New(pool, engine),
		Rules:  &ruleadmin.Service{Pool: pool, Chain: chain},
		Alerts: &alerts.Service{Pool: pool, Chain: chain, Engine: engine},
		Chain:  chain,
		Engine: engine,
	}
	return &Harness{
		T: t, Pool: pool, Org: org, Keys: keys,
		Router: api.NewRouter(deps), Deps: deps,
	}
}

// CreateRule inserts a rule version directly, bypassing HTTP.
func (h *Harness) CreateRule(ruleType string, params map[string]any, effectiveAt time.Time) db.RuleVersion {
	h.T.Helper()
	raw, _ := json.Marshal(params)
	q := db.New(h.Pool)
	next, err := q.NextRuleVersion(context.Background(), db.NextRuleVersionParams{
		OrgID: h.Org.ID, RuleType: ruleType,
	})
	if err != nil {
		h.T.Fatalf("next version: %v", err)
	}
	rv, err := q.CreateRuleVersion(context.Background(), db.CreateRuleVersionParams{
		OrgID:       h.Org.ID,
		RuleType:    ruleType,
		Version:     next,
		IsActive:    true,
		Params:      raw,
		EffectiveAt: pgtype.Timestamptz{Time: effectiveAt.UTC(), Valid: true},
	})
	if err != nil {
		h.T.Fatalf("create rule: %v", err)
	}
	return rv
}

// Do issues an authenticated JSON request.
func (h *Harness) Do(method, path, key string, body any) (*http.Response, map[string]any) {
	h.T.Helper()
	rec := h.doRaw(method, path, key, body)
	var out map[string]any
	if rec.Body.Len() > 0 {
		_ = json.Unmarshal(rec.Body.Bytes(), &out)
	}
	return rec.Result(), out
}

func (h *Harness) doRaw(method, path, key string, body any) *httptest.ResponseRecorder {
	var buf bytes.Buffer
	if body != nil {
		if err := json.NewEncoder(&buf).Encode(body); err != nil {
			h.T.Fatal(err)
		}
	}
	req := httptest.NewRequest(method, path, &buf)
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.Router.ServeHTTP(rec, req)
	return rec
}

// MustStatus fails the test when the status differs and returns the decoded body.
func (h *Harness) MustStatus(method, path, key string, want int, body any) map[string]any {
	h.T.Helper()
	rec := h.doRaw(method, path, key, body)
	if rec.Code != want {
		h.T.Fatalf("%s %s: got status %d, want %d; body=%s",
			method, path, rec.Code, want, rec.Body.String())
	}
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return out
}

func (h *Harness) Alerts(status string) []any {
	h.T.Helper()
	path := "/v1/alerts/"
	if status != "" {
		path += "?status=" + status
	}
	_, out := h.Do(http.MethodGet, path, h.Keys.Analyst, nil)
	raw, _ := json.Marshal(out["alerts"])
	var list []any
	_ = json.Unmarshal(raw, &list)
	return list
}

// IngestEvents is a shorthand for the collector batch endpoint.
func (h *Harness) IngestEvents(source string, evs []map[string]any) (*http.Response, map[string]any) {
	return h.Do(http.MethodPost, "/v1/events:batch", h.Keys.Collector,
		map[string]any{"source": source, "events": evs})
}

// EventCount counts stored events for the org.
func (h *Harness) EventCount() int64 {
	h.T.Helper()
	n, err := db.New(h.Pool).CountEventsByOrg(context.Background(), db.CountEventsByOrgParams{
		OrgID: h.Org.ID,
	})
	if err != nil {
		h.T.Fatal(err)
	}
	return n
}

func randHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// RFC3339 must is a small timestamp helper.
func rfc(t time.Time) string { return t.UTC().Format(time.RFC3339) }

func pgxTS(t time.Time) pgtype.Timestamptz {
	return pgtype.Timestamptz{Time: t.UTC(), Valid: true}
}

var _ = fmt.Sprintf
