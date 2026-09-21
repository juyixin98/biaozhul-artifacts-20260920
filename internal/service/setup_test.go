package service_test

import (
	"context"
	"fmt"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"communityvault/internal/auth"
	"communityvault/internal/migrate"
	"communityvault/internal/service"
	"communityvault/internal/testdb"
)

const (
	alice   int64 = 1 // member
	bob     int64 = 2 // member
	modTech int64 = 3 // moderator, scope: tech
	modArt  int64 = 4 // moderator, scope: art
	adminID int64 = 5
)

var (
	pool     *pgxpool.Pool
	svc      *service.Service
	svcShort *service.Service // claim TTL = 1s for expiry tests
	ctx      = context.Background()
)

func TestMain(m *testing.M) {
	p, _, err := testdb.Setup(context.Background(), "service", migrate.Up)
	if err != nil {
		fmt.Fprintf(os.Stderr, "testdb setup: %v\n", err)
		if os.Getenv("TEST_DB_REQUIRED") == "1" {
			os.Exit(1)
		}
		fmt.Fprintln(os.Stderr, "SKIP: set TEST_DATABASE_URL to run integration tests")
		os.Exit(0)
	}
	pool = p
	svc = service.New(pool, 30*time.Second)
	svcShort = service.New(pool, 1*time.Second)
	os.Exit(m.Run())
}

func principal(id int64) *auth.Principal {
	p := &auth.Principal{ID: id, Role: "member", Scopes: map[string]bool{}}
	switch id {
	case modTech:
		p.Role, p.Name = "moderator", "mod_tech"
		p.Scopes["tech"] = true
	case modArt:
		p.Role, p.Name = "moderator", "mod_art"
		p.Scopes["art"] = true
	case adminID:
		p.Role, p.Name = "admin", "admin"
	default:
		p.Name = fmt.Sprintf("user%d", id)
	}
	return p
}

var uniq int64
var uniqMu sync.Mutex

func uniqueSuffix() string {
	uniqMu.Lock()
	defer uniqMu.Unlock()
	uniq++
	return fmt.Sprintf("t%d-%d", time.Now().UnixNano(), uniq)
}

func mustCreateDraft(t *testing.T, authorID int64, category, body string) service.ContentDTO {
	t.Helper()
	suf := uniqueSuffix()
	c, err := svc.Create(ctx, principal(authorID), service.CreateContentInput{
		Category: category, Title: "post " + suf, Body: body, EditReason: "initial",
	})
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	return c
}

// initTest registers a full reset after the test so the shared database never
// leaks queue rows, rule versions or content between tests.
func initTest(t *testing.T) {
	t.Helper()
	t.Cleanup(func() {
		if err := resetDB(context.Background()); err != nil {
			t.Fatalf("reset db: %v", err)
		}
	})
}

func resetDB(ctx context.Context) error {
	_, err := pool.Exec(ctx, `
		TRUNCATE reports, status_events, moderation_decisions, review_tasks,
		         content_revisions, contents, rule_words, rule_versions,
		         moderator_scopes, users
		RESTART IDENTITY CASCADE`)
	if err != nil {
		return err
	}
	_, err = pool.Exec(ctx, `
		INSERT INTO users (id, username, role) VALUES
			(1,'alice','member'),(2,'bob','member'),
			(3,'mod_tech','moderator'),(4,'mod_art','moderator'),
			(5,'admin','admin');
		INSERT INTO moderator_scopes (user_id, category) VALUES (3,'tech'),(4,'art');
		INSERT INTO rule_versions (id, version, status, description, created_by)
			VALUES (1,1,'active','baseline word list',5);
		INSERT INTO rule_words (rule_version_id, word) VALUES (1,'forbidden');
		SELECT setval(pg_get_serial_sequence('users','id'), (SELECT max(id) FROM users));
		SELECT setval(pg_get_serial_sequence('rule_versions','id'), (SELECT max(id) FROM rule_versions));
	`)
	return err
}

func sleepForReclaim() { time.Sleep(1200 * time.Millisecond) }
