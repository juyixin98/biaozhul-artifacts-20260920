// Package testsupport provides schema-isolated PostgreSQL fixtures for the
// integration tests. Each test gets a fresh schema in TEST_DATABASE_URL
// (default: local desklens_test), so tests never see each other's data.
package testsupport

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net/url"
	"os"
	"sync/atomic"
	"testing"

	"github.com/jmoiron/sqlx"

	"desklens/internal/admin"
	"desklens/internal/db"
	"desklens/internal/ingest"
	"desklens/internal/manager"
	"desklens/internal/store"
)

var schemaCounter int64

func testDSN(schema string) string {
	base := os.Getenv("TEST_DATABASE_URL")
	if base == "" {
		base = "postgres://desklens:desklens@localhost:5432/desklens_test?sslmode=disable"
	}
	u, err := url.Parse(base)
	if err != nil {
		panic(err)
	}
	q := u.Query()
	q.Set("search_path", schema)
	u.RawQuery = q.Encode()
	return u.String()
}

// SetupDB creates an isolated migrated+seeded schema and returns a pool
// pointed at it. Tests are skipped when the database is unreachable.
func SetupDB(t *testing.T) *sqlx.DB {
	t.Helper()

	adminDSN := testDSN("public")
	root, err := db.Connect(context.Background(), adminDSN)
	if err != nil {
		t.Skipf("integration database unavailable (%v); set TEST_DATABASE_URL to run", err)
	}

	var b [4]byte
	_, _ = rand.Read(b[:])
	n := atomic.AddInt64(&schemaCounter, 1)
	schema := fmt.Sprintf("t_%d_%s", n, hex.EncodeToString(b[:]))

	if _, err := root.Exec(`create schema ` + schema + ` authorization desklens`); err != nil {
		_ = root.Close()
		t.Fatalf("create schema: %v", err)
	}
	t.Cleanup(func() {
		_, _ = root.Exec(`drop schema if exists ` + schema + ` cascade`)
		_ = root.Close()
	})

	d, err := db.Connect(context.Background(), testDSN(schema))
	if err != nil {
		t.Fatalf("connect test schema: %v", err)
	}
	t.Cleanup(func() { _ = d.Close() })

	ctx := context.Background()
	if err := db.Migrate(ctx, d); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	if err := db.Seed(ctx, d); err != nil {
		t.Fatalf("seed: %v", err)
	}
	return d
}

type Services struct {
	DB      *sqlx.DB
	Store   *store.Store
	Ingest  *ingest.Service
	Manager *manager.Service
	Admin   *admin.Service
}

func NewServices(d *sqlx.DB) Services {
	st := store.New(d)
	return Services{
		DB:      d,
		Store:   st,
		Ingest:  ingest.New(d, st),
		Manager: manager.New(d),
		Admin:   admin.New(d),
	}
}

func MustExec(t *testing.T, d sqlx.ExecerContext, query string, args ...any) {
	t.Helper()
	if _, err := d.ExecContext(context.Background(), query, args...); err != nil {
		t.Fatalf("exec %q: %v", query, err)
	}
}

// RawCount returns the number of stored snapshots for a workstation.
func RawCount(t *testing.T, d *sqlx.DB, workstation string) int {
	t.Helper()
	var n int
	if err := d.Get(&n,
		`select count(*) from activity_snapshots where workstation_id=$1`, workstation); err != nil {
		t.Fatal(err)
	}
	return n
}

// DailyRow is a queried daily summary row.
type DailyRow struct {
	EmployeeID        int64  `db:"employee_id"`
	LocalDate         string `db:"local_date"`
	ProductiveCount   int64  `db:"productive_count"`
	UnproductiveCount int64  `db:"unproductive_count"`
	NeutralCount      int64  `db:"neutral_count"`
	TotalCount        int64  `db:"total_count"`
	CvMin             int64  `db:"cv_min"`
	CvMax             int64  `db:"cv_max"`
	PvMin             int32  `db:"pv_min"`
	PvMax             int32  `db:"pv_max"`
	Frozen            bool   `db:"frozen"`
}

func GetDaily(t *testing.T, d *sqlx.DB, empID int64, date string) (DailyRow, bool) {
	t.Helper()
	var r DailyRow
	err := d.Get(&r, `
		select employee_id, local_date::text as local_date,
		       productive_count, unproductive_count, neutral_count, total_count,
		       classification_version_min as cv_min, classification_version_max as cv_max,
		       policy_version_min as pv_min, policy_version_max as pv_max, frozen
		from daily_employee_summary where employee_id=$1 and local_date=$2::date`,
		empID, date)
	if err != nil {
		return DailyRow{}, false
	}
	return r, true
}

type WeeklyRow struct {
	DepartmentID      int64  `db:"department_id"`
	WeekStart         string `db:"week_start"`
	ProductiveCount   int64  `db:"productive_count"`
	UnproductiveCount int64  `db:"unproductive_count"`
	NeutralCount      int64  `db:"neutral_count"`
	TotalCount        int64  `db:"total_count"`
	Frozen            bool   `db:"frozen"`
}

func GetWeekly(t *testing.T, d *sqlx.DB, deptID int64, weekStart string) (WeeklyRow, bool) {
	t.Helper()
	var r WeeklyRow
	err := d.Get(&r, `
		select department_id, week_start::text as week_start,
		       productive_count, unproductive_count, neutral_count, total_count, frozen
		from weekly_department_summary where department_id=$1 and week_start=$2::date`,
		deptID, weekStart)
	if err != nil {
		return WeeklyRow{}, false
	}
	return r, true
}
