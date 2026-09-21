package integration

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"sync/atomic"
	"testing"

	"proofcycle/internal/db"
	"proofcycle/internal/models"
	"proofcycle/internal/storage"

	"gorm.io/gorm"
)

// userSeq guarantees unique usernames even when a helper is called multiple
// times within one test.
var userSeq uint64

// testDSN comes from PROOFCYCLE_TEST_DSN and defaults to the local docker
// MySQL mapped to 127.0.0.1:23306 (see README "Running the tests").
func testDSN() string {
	if dsn := os.Getenv("PROOFCYCLE_TEST_DSN"); dsn != "" {
		return dsn
	}
	return "proof:proof@tcp(127.0.0.1:23306)/proofcycle_test?charset=utf8mb4&parseTime=True&loc=UTC&multiStatements=true"
}

// Env bundles everything one integration test needs.
type Env struct {
	DB    *gorm.DB
	Store *storage.Store
}

// setupEnv opens the test database, applies migrations and gives every test a
// clean schema and empty storage directory.
func setupEnv(t *testing.T) *Env {
	t.Helper()

	dsn := testDSN()
	gdb, err := db.Open(dsn)
	if err != nil {
		t.Skipf("MySQL not reachable at %q: %v (set PROOFCYCLE_TEST_DSN or run docker compose)", dsn, err)
	}
	if err := db.Ping(gdb); err != nil {
		t.Skipf("MySQL ping failed: %v", err)
	}

	// Wipe every table so test order never matters.
	drop := []string{
		"approvals", "opinions", "round_items", "rounds",
		"file_versions", "job_reviewers", "checklist_template_items",
		"jobs", "users", "schema_migrations",
	}
	for _, table := range drop {
		if err := gdb.Exec("DROP TABLE IF EXISTS " + table).Error; err != nil {
			t.Fatalf("drop %s: %v", table, err)
		}
	}
	if err := db.Migrate(gdb); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	store, err := storage.New(t.TempDir(), 1<<20)
	if err != nil {
		t.Fatalf("storage: %v", err)
	}
	return &Env{DB: gdb, Store: store}
}

// createUser persists a user with the given role and a unique username/token.
// The caller's preferred username is suffixed with a process-wide counter so
// repeated helper calls inside one test never collide.
func createUser(t *testing.T, gdb *gorm.DB, username, role string) *models.User {
	t.Helper()
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil {
		t.Fatal(err)
	}
	n := atomic.AddUint64(&userSeq, 1)
	u := models.User{
		Username:     fmt.Sprintf("%s-%d", username, n),
		PasswordHash: "$2a$10$invalidinvalidinvalidinvalidinvalidin", // never logged in during tests
		Role:         role,
		Token:        hex.EncodeToString(b[:]),
	}
	if err := gdb.Create(&u).Error; err != nil {
		t.Fatalf("create user %s: %v", username, err)
	}
	return &u
}
