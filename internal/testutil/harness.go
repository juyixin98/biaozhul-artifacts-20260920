// Package testutil provisions an isolated, migrated PostgreSQL database and a
// temp object-store directory for integration tests.
package testutil

import (
	"context"
	"fmt"
	"net/url"
	"os"
	"sync/atomic"
	"testing"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/jmoiron/sqlx"

	"synapticgo/internal/db"
	"synapticgo/internal/store"
)

var seq int64

// Env is a wired test environment.
type Env struct {
	T       *testing.T
	AdminDB *sqlx.DB // connected to a maintenance DB (create/drop test DBs)
	DB      *sqlx.DB // connected to the isolated test DB
	DataDir string
	Store   *store.FileStore
	Objects *store.ObjectService
}

// adminURL returns a connection to the "postgres" maintenance database using
// the same credentials as DATABASE_URL.
func adminURL() string {
	raw := os.Getenv("TEST_DATABASE_URL")
	if raw == "" {
		raw = "postgres://synaptic:synaptic@localhost:5432/synapticgo?sslmode=disable"
	}
	u, err := url.Parse(raw)
	u.Path = "/postgres"
	if err != nil {
		panic(err)
	}
	return u.String()
}

// New creates a fresh database with migrations applied.
func New(t *testing.T) *Env {
	t.Helper()
	admin, err := sqlx.Connect("pgx", adminURL())
	if err != nil {
		t.Fatalf("connect admin db: %v", err)
	}

	n := atomic.AddInt64(&seq, 1)
	name := fmt.Sprintf("syn_test_%d_%d", time.Now().UnixNano(), n)
	var ownerRole string
	if err := admin.Get(&ownerRole, `SELECT current_user`); err != nil {
		admin.Close()
		t.Fatalf("current_user: %v", err)
	}
	if _, err := admin.Exec(`CREATE DATABASE ` + name + ` OWNER ` + ownerRole); err != nil {
		admin.Close()
		t.Fatalf("create database %s: %v", name, err)
	}
	t.Cleanup(func() {
		admin.Exec(`DROP DATABASE IF EXISTS ` + name)
		admin.Close()
	})

	u, _ := url.Parse(adminURL())
	u.Path = "/" + name
	conn, err := sqlx.Connect("pgx", u.String())
	if err != nil {
		t.Fatalf("connect test db: %v", err)
	}
	t.Cleanup(func() { conn.Close() })
	conn.SetMaxOpenConns(10)

	if err := db.Migrate(conn); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	dir := t.TempDir()
	fs, err := store.New(dir)
	if err != nil {
		t.Fatalf("file store: %v", err)
	}

	return &Env{
		T:       t,
		AdminDB: admin,
		DB:      conn,
		DataDir: dir,
		Store:   fs,
		Objects: store.NewObjectService(conn, fs),
	}
}

// Restart runs startup recovery on a fresh FileStore, simulating a process
// restart.
func (e *Env) Restart() {
	e.T.Helper()
	fs, err := store.New(e.DataDir)
	if err != nil {
		e.T.Fatal(err)
	}
	if err := store.Recover(e.DB, fs); err != nil {
		e.T.Fatalf("recover: %v", err)
	}
	e.Store = fs
	e.Objects = store.NewObjectService(e.DB, fs)
}

// MustExec is a small context-bearing helper.
func (e *Env) MustExec(query string, args ...any) {
	e.T.Helper()
	if _, err := e.DB.ExecContext(context.Background(), query, args...); err != nil {
		e.T.Fatalf("exec: %v\nquery: %s", err, query)
	}
}
