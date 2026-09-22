package integration

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/modules/postgres"
	"github.com/testcontainers/testcontainers-go/wait"

	"community-governance/internal/app"
	"community-governance/internal/migrate"
)

var (
	pool    *pgxpool.Pool
	baseURL string
	client  = &http.Client{Timeout: 15 * time.Second}
)

func TestMain(m *testing.M) {
	ctx := context.Background()
	pgC, err := postgres.RunContainer(ctx,
		testcontainers.WithImage("postgres:16-alpine"),
		postgres.WithDatabase("govtest"),
		postgres.WithUsername("test"),
		postgres.WithPassword("test"),
		testcontainers.WithWaitStrategy(
			wait.ForLog("database system is ready to accept connections").
				WithOccurrence(2).WithStartupTimeout(60*time.Second)),
	)
	if err != nil {
		log.Fatalf("start postgres container: %v", err)
	}
	connStr, err := pgC.ConnectionString(ctx, "sslmode=disable")
	if err != nil {
		log.Fatal(err)
	}

	pool, err = pgxpool.New(ctx, connStr)
	if err != nil {
		log.Fatal(err)
	}
	conn, err := pool.Acquire(ctx)
	if err != nil {
		log.Fatal(err)
	}
	if err := migrate.Migrate(ctx, conn.Conn()); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	conn.Release()

	// Test server with a synthetic platform bootstrap token used to create
	// isolated communities per test.
	srv := httptest.NewServer(app.NewWithBootstrapToken(pool, "tok_bootstrap").Router())
	baseURL = srv.URL

	code := m.Run()
	srv.Close()
	_ = pgC.Terminate(ctx)
	os.Exit(code)
}

// ---------------------------------------------------------------------------
// JSON HTTP helpers
// ---------------------------------------------------------------------------

func doJSON(t *testing.T, method, path, token string, body any) (int, map[string]any) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, baseURL+path, rdr)
	if err != nil {
		t.Fatal(err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out map[string]any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	if out == nil {
		out = map[string]any{}
	}
	out["__status"] = resp.StatusCode
	return resp.StatusCode, out
}

func mustStatus(t *testing.T, got, want int, body map[string]any) {
	t.Helper()
	if got != want {
		t.Fatalf("status = %d, want %d: %v", got, want, body)
	}
}

func asInt64(v any) int64 {
	switch n := v.(type) {
	case float64:
		return int64(n)
	case json.Number:
		i, _ := n.Int64()
		return i
	}
	return 0
}

// ---------------------------------------------------------------------------
// Domain scenario builders (each test gets an isolated community)
// ---------------------------------------------------------------------------

type env struct {
	t         *testing.T
	cid       int64
	admin     string
	adminID   int64
	moderator string
	modID     int64
}

func newEnv(t *testing.T) *env {
	t.Helper()
	st, body := doJSON(t, http.MethodPost, "/admin/communities", "tok_bootstrap", map[string]any{
		"name":       fmt.Sprintf("community %s", t.Name()),
		"admin_name": "owner",
	})
	mustStatus(t, st, 201, body)
	return &env{
		t:       t,
		cid:     asInt64(body["community"].(map[string]any)["id"]),
		admin:   body["admin_token"].(string),
		adminID: asInt64(body["admin"].(map[string]any)["id"]),
	}
}

func (e *env) createUser(name, role string) (int64, string) {
	st, body := doJSON(e.t, http.MethodPost,
		fmt.Sprintf("/communities/%d/users", e.cid), e.admin,
		map[string]any{"username": name, "role": role})
	mustStatus(e.t, st, 201, body)
	u := body["user"].(map[string]any)
	return asInt64(u["id"]), body["token"].(string)
}

func (e *env) moderatorUser() (int64, string) {
	id, tok := e.createUser("reviewer", "moderator")
	e.moderator, e.modID = tok, id
	return id, tok
}

func (e *env) tier(level int32, price int64, days int32) int64 {
	st, body := doJSON(e.t, http.MethodPost,
		fmt.Sprintf("/communities/%d/tiers", e.cid), e.admin,
		map[string]any{
			"level": level, "name": fmt.Sprintf("T%d", level),
			"price_cents": price, "duration_days": days,
		})
	mustStatus(e.t, st, 201, body)
	return asInt64(body["id"])
}

// pay registers an offline payment for a member and returns the response.
func (e *env) pay(requestID string, userID, tierID int64, amount int64, days int32) (int, map[string]any) {
	return doJSON(e.t, http.MethodPost,
		fmt.Sprintf("/communities/%d/payments", e.cid), e.admin, map[string]any{
			"request_id": requestID, "user_id": userID, "tier_id": tierID,
			"amount_cents": amount, "days": days,
		})
}

type contentRef struct {
	id       int64
	v1       int64
	author   string
	authorID int64
}

// authorContent creates a member-author + draft content with first version.
func (e *env) authorContent(name string, level int32, bodyText string) contentRef {
	id, tok := e.createUser(name, "member")
	st, b := doJSON(e.t, http.MethodPost,
		fmt.Sprintf("/communities/%d/contents", e.cid), tok,
		map[string]any{"title": name + " post", "body": bodyText, "required_level": level})
	mustStatus(e.t, st, 201, b)
	cid := asInt64(b["id"])
	// Fetch v1 id.
	_, vb := doJSON(e.t, http.MethodGet, fmt.Sprintf("/contents/%d/versions", cid), tok, nil)
	v1 := asInt64(vb["versions"].([]any)[0].(map[string]any)["id"])
	return contentRef{id: cid, v1: v1, author: tok, authorID: id}
}

func (e *env) submit(c contentRef) {
	st, b := doJSON(e.t, http.MethodPost, fmt.Sprintf("/contents/%d/submit", c.id), c.author,
		map[string]any{"version_id": c.v1})
	mustStatus(e.t, st, 200, b)
}

func (e *env) approve(tok string, c contentRef, version int64, reason string) (int, map[string]any) {
	return doJSON(e.t, http.MethodPost, fmt.Sprintf("/contents/%d/approve", c.id), tok,
		map[string]any{"version_id": version, "reason": reason})
}

// publishFlow creates author content, submits and has the moderator approve.
func (e *env) publishFlow(modTok string, level int32, bodyText string) contentRef {
	c := e.authorContent("author-"+bodyText, level, bodyText)
	e.submit(c)
	st, b := e.approve(modTok, c, c.v1, "ok")
	mustStatus(e.t, st, 200, b)
	return c
}

func (e *env) addVersion(c contentRef, bodyText string) (int64, int, map[string]any) {
	st, b := doJSON(e.t, http.MethodPost, fmt.Sprintf("/contents/%d/versions", c.id), c.author,
		map[string]any{"body": bodyText})
	vid := int64(0)
	if v, ok := b["version"]; ok {
		vid = asInt64(v.(map[string]any)["id"])
	}
	return vid, st, b
}

func fetchSubscription(t *testing.T, e *env, userID int64) map[string]any {
	t.Helper()
	var status string
	var periodEnd time.Time
	err := pool.QueryRow(context.Background(),
		`SELECT status, period_end FROM subscriptions
		 WHERE community_id=$1 AND user_id=$2`, e.cid, userID).
		Scan(&status, &periodEnd)
	if err != nil {
		t.Fatal(err)
	}
	return map[string]any{
		"status":     status,
		"period_end": periodEnd.Format(time.RFC3339Nano),
	}
}
