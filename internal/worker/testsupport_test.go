package worker

import (
	"context"
	"os"
	"sync"
	"testing"
	"time"

	"gorm.io/gorm"

	"sitevitals/internal/models"
	"sitevitals/internal/store"
)

type gormDB = *gorm.DB

var testDBMu sync.Mutex // serialise tests that share and wipe the test schema

// storeTestDB opens the MySQL test schema, wipes it and returns the DB plus a
// cleanup that waits for the pool to stop before tables are cleaned.
func storeTestDB(t *testing.T) *gorm.DB {
	t.Helper()
	testDBMu.Lock()
	t.Cleanup(testDBMu.Unlock)

	dsn := os.Getenv("SV_TEST_DSN")
	if dsn == "" {
		dsn = "root:@tcp(127.0.0.1:3306)/sitevitals_b_worker_test?charset=utf8mb4&parseTime=True&loc=UTC&multiStatements=true"
	}
	db, err := store.Open(dsn)
	if err != nil {
		t.Skipf("mysql not available: %v", err)
	}
	if err := store.AutoMigrate(db); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	wipeDB(t, db)
	return db
}

func wipeDB(t *testing.T, db *gorm.DB) {
	t.Helper()
	for _, m := range models.AllModels() {
		stmt := &gorm.Statement{DB: db}
		if err := stmt.Parse(m); err != nil {
			t.Fatal(err)
		}
		if err := db.Exec("DELETE FROM " + stmt.Schema.Table).Error; err != nil {
			t.Fatalf("cleanup: %v", err)
		}
	}
}

// runningPool starts a pool and returns it together with a stop function that
// cancels the context and blocks until all worker/reaper goroutines exit, so a
// later test can never observe a still-polling pool.
func runningPool(t *testing.T, cfg configType, db *gorm.DB, exec Executor) (*Pool, *store.Queue, *store.Repo, func()) {
	t.Helper()
	q := store.NewQueue(db)
	repo := store.NewRepo(db)
	pool := NewPool(cfg, q, repo, nil, exec)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		pool.Run(ctx)
		close(done)
	}()
	stop := func() {
		cancel()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Log("pool did not stop within 5s")
		}
	}
	return pool, q, repo, stop
}
