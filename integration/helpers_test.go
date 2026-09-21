// Integration tests exercising the service layer against real PostgreSQL.
// Run with:
//
//	TEST_DATABASE_URL=postgres://desklens:desklens@localhost:5432/desklens_test?sslmode=disable \
//	go test ./...
//
// Without a reachable database the integration tests skip.
package integration

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"

	"desklens/internal/admin"
	"desklens/internal/ingest"
	"desklens/internal/manager"
	"desklens/internal/model"
	"desklens/internal/testsupport"
)

type env struct {
	testsupport.Services
	t *testing.T
}

func setup(t *testing.T) *env {
	t.Helper()
	d := testsupport.SetupDB(t)
	return &env{Services: testsupport.NewServices(d), t: t}
}

// Principal builders for seeded workstations.
func wsPrincipal(ws string, empID, deptID int64, tz string) ingest.Principal {
	return ingest.Principal{WorkstationID: ws, EmployeeID: empID, DepartmentID: deptID, Timezone: tz}
}

var (
	p101 = wsPrincipal("WS-101", 101, 1, "America/New_York")
	p102 = wsPrincipal("WS-102", 102, 1, "Asia/Shanghai")
	p103 = wsPrincipal("WS-103", 103, 2, "Europe/Berlin")
	p104 = wsPrincipal("WS-104", 104, 4, "America/New_York") // exempt dept
)

// snapAt builds a snapshot at the given UTC minute.
func snapAt(ws string, empID int64, utc time.Time, app string, count int) model.Snapshot {
	return model.Snapshot{
		WorkstationID: ws,
		EmployeeID:    empID,
		UTCTime:       utc.Truncate(time.Minute),
		AppName:       app,
		ActivityCount: count,
	}
}

func utc(y int, mo time.Month, d, h, mi int) time.Time {
	return time.Date(y, mo, d, h, mi, 0, 0, time.UTC)
}

func managerPrincipal(deptID int64) manager.Principal {
	return manager.Principal{Username: "mgr", DepartmentID: deptID}
}

func (e *env) mustIngest(p ingest.Principal, batchTag string, snaps ...model.Snapshot) *ingest.Outcome {
	e.t.Helper()
	// Deterministically map human-readable test tags to UUIDs so replays work.
	batchID := uuid.NewSHA1(uuid.NameSpaceURL, []byte("desklens-test:"+batchTag)).String()
	out, err := e.Ingest.Ingest(context.Background(), p, ingest.Request{
		BatchID: &batchID, Snapshots: snaps,
	})
	if err != nil {
		e.t.Fatalf("ingest batch %s: %v", batchTag, err)
	}
	return out
}

// ingestErr runs ingest and asserts an API error with the given code.
func (e *env) ingestErr(p ingest.Principal, snaps []model.Snapshot, wantCode string) *ingest.APIError {
	e.t.Helper()
	_, err := e.Ingest.Ingest(context.Background(), p, ingest.Request{Snapshots: snaps})
	if err == nil {
		e.t.Fatalf("expected error %s, got success", wantCode)
	}
	ae, ok := err.(*ingest.APIError)
	if !ok {
		e.t.Fatalf("expected ingest.APIError, got %T: %v", err, err)
	}
	if ae.Code != wantCode {
		e.t.Fatalf("error code = %s, want %s (%s)", ae.Code, wantCode, ae.Message)
	}
	return ae
}

// publishWidePolicy publishes an all-day window (00:00-24:00 local) so cross-day
// tests can send any UTC minute; exclusion/exempt config is inherited explicitly.
func (e *env) publishWidePolicy(start, end string) int32 {
	e.t.Helper()
	out, err := e.Admin.PublishPolicy(context.Background(), admin.PolicyInput{
		MonitoringStart:   start,
		MonitoringEnd:     end,
		ExcludedApps:      []string{"1password*", "keepass*", "*secret-manager*", "*vault*"},
		ExemptDepartments: []int64{4},
		PublishedBy:       "test",
	})
	if err != nil {
		e.t.Fatalf("publish policy: %v", err)
	}
	return out.Version
}

func (e *env) mustRebuild(start, end string) *admin.RebuildOut {
	e.t.Helper()
	out, err := e.Admin.Rebuild(context.Background(), admin.RebuildInput{StartDate: start, EndDate: end})
	if err != nil {
		e.t.Fatalf("rebuild %s..%s: %v", start, end, err)
	}
	return out
}

func (e *env) rawRow(ws string, bucket time.Time) (app string, count int, ok bool) {
	e.t.Helper()
	err := e.DB.QueryRowx(`
		select app_name, activity_count from activity_snapshots
		where workstation_id=$1 and bucket_time=$2`, ws, bucket).Scan(&app, &count)
	if err != nil {
		return "", 0, false
	}
	return app, count, true
}

func background() context.Context { return context.Background() }

func adminRebuildInput() admin.RebuildInput {
	return admin.RebuildInput{StartDate: "2026-09-15", EndDate: "2026-09-15"}
}
