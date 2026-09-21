package integrationtest

import (
	"context"
	"testing"

	"desklens/internal/repo"
	"desklens/internal/timeutil"
)

// Privacy filtering: excluded apps and exempt-department staff are accepted
// by the API but must never reach the raw table or any statistic.
func TestPrivacyFiltering(t *testing.T) {
	env := NewEnv(t)
	ctx := context.Background()

	out, err := env.Ingest.Ingest(ctx, []repo.SnapshotIn{
		snap(1001, 101, "2026-03-10T10:00:00Z", "VS Code", 40),
		snap(1001, 101, "2026-03-10T10:01:00Z", "1Password", 99),
		snap(1004, 104, "2026-03-10T10:00:00Z", "Legal CRM", 25), // Legal exempt
	})
	if err != nil {
		t.Fatalf("ingest: %v", err)
	}
	if out.Accepted != 1 || out.Filtered.ExcludedApp != 1 || out.Filtered.ExemptDepartment != 1 {
		t.Fatalf("unexpected outcome: %+v", out)
	}

	rows, err := env.Repo.ListRaw(ctx, repo.RawFilter{Limit: 100})
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 {
		t.Fatalf("raw rows = %d, want 1: %+v", len(rows), rows)
	}
	if rows[0].AppName != "VS Code" {
		t.Fatalf("filtered app leaked into raw table: %q", rows[0].AppName)
	}

	daily, err := env.Repo.ListDaily(ctx, repo.SummaryFilter{Limit: 100})
	if err != nil {
		t.Fatal(err)
	}
	if len(daily) != 1 {
		t.Fatalf("daily rows = %d, want 1", len(daily))
	}
	if daily[0].EmployeeID == 104 {
		t.Fatalf("exempt department leaked into daily summary")
	}
}

// Outside-window data is dropped before storage. Window is 09:00-18:00 local
// for employee 101 (Europe/London): 20:00 local is outside.
func TestOutsideWindow(t *testing.T) {
	env := NewEnv(t)
	out, err := env.Ingest.Ingest(context.Background(), []repo.SnapshotIn{
		snap(1001, 101, "2026-03-10T20:00:00Z", "VS Code", 5), // 20:00 London
	})
	if err != nil {
		t.Fatal(err)
	}
	if out.Accepted != 0 || out.Filtered.OutsideWindow != 1 {
		t.Fatalf("window filter not applied: %+v", out)
	}
}

// Cross-day: a UTC minute after local midnight that is still the previous
// calendar day inside the employee's monitoring window must be attributed to
// the previous local date. Grace is in Los Angeles: 2026-03-11 00:00 UTC is
// 2026-03-10 16:00 PDT (UTC-7 after the 2026-03-08 spring-forward) — inside
// the 09-18 window, on the 10th local date.
func TestCrossDayTimezone(t *testing.T) {
	env := NewEnv(t)
	ctx := context.Background()
	_, err := env.Ingest.Ingest(ctx, []repo.SnapshotIn{
		snap(1003, 103, "2026-03-11T00:00:00Z", "Zoom Meetings", 22),
	})
	if err != nil {
		t.Fatal(err)
	}
	rows, err := env.Repo.ListRaw(ctx, repo.RawFilter{Limit: 10})
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 {
		t.Fatalf("rows = %d, want 1 (00:00Z = 16:00 PST on 2026-03-10)", len(rows))
	}
	wantDate := date("2026-03-10")
	if !rows[0].LocalDate.Equal(wantDate) {
		t.Fatalf("local_date = %s, want 2026-03-10 (America/Los_Angeles)", rows[0].LocalDate)
	}
	wantWeek := date("2026-03-09") // Monday of the local week
	if !timeutil.MondayOfWeek(rows[0].LocalDate).Equal(wantWeek) {
		t.Fatalf("derived week = %s, want 2026-03-09", timeutil.MondayOfWeek(rows[0].LocalDate))
	}

	// Weekly summary attributes the row to the local-date week 2026-03-09.
	var w repo.WeeklySummary
	if err := env.DB.Get(&w,
		`SELECT * FROM department_weekly_summary
		  WHERE department_id = 2 AND iso_week = DATE '2026-03-09'`); err != nil {
		t.Fatalf("weekly bucket wrong: %v", err)
	}
	if w.NonProductiveCount != 22 {
		t.Fatalf("weekly non-productive = %d, want 22", w.NonProductiveCount)
	}
}

// Duplicate content is processed exactly once; a conflicting count rejects
// the whole batch and rolls back every row in it.
func TestIdempotencyAndConflict(t *testing.T) {
	env := NewEnv(t)
	ctx := context.Background()
	batch := []repo.SnapshotIn{
		snap(1001, 101, "2026-03-10T10:00:00Z", "VS Code", 40),
	}
	out, err := env.Ingest.Ingest(ctx, batch)
	if err != nil {
		t.Fatal(err)
	}
	if out.Accepted != 1 {
		t.Fatalf("first accept = %d", out.Accepted)
	}
	// Identical repeat -> duplicate, accepted stays 0.
	out, err = env.Ingest.Ingest(ctx, batch)
	if err != nil {
		t.Fatal(err)
	}
	if out.Accepted != 0 || out.Duplicate != 1 {
		t.Fatalf("repeat: %+v", out)
	}

	// Conflict mixed with a brand-new row: whole batch rolls back, the new
	// row must not persist.
	_, err = env.Ingest.Ingest(ctx, []repo.SnapshotIn{
		snap(1001, 101, "2026-03-10T10:00:00Z", "VS Code", 999), // conflict
		snap(1001, 101, "2026-03-10T10:05:00Z", "Slack", 10),    // would be new
	})
	if !isConflict(err) {
		t.Fatalf("want conflict, got %v", err)
	}
	rows, _ := env.Repo.ListRaw(ctx, repo.RawFilter{Limit: 100})
	if len(rows) != 1 {
		t.Fatalf("batch did not roll back: %d raw rows", len(rows))
	}
	if rows[0].ActivityCount != 40 {
		t.Fatalf("conflicting value overwrote stored count: %d", rows[0].ActivityCount)
	}
}

// Structural validation failures abort the batch before any write.
func TestValidationRollback(t *testing.T) {
	env := NewEnv(t)
	_, err := env.Ingest.Ingest(context.Background(), []repo.SnapshotIn{
		snap(1001, 101, "2026-03-10T10:00:00Z", "VS Code", 40),
		snap(0, 101, "2026-03-10T10:01:00Z", "VS Code", 1), // bad workstation
	})
	if err == nil {
		t.Fatal("expected validation error")
	}
	if !isValidation(err) {
		t.Fatalf("want validation error, got %v", err)
	}
	rows, _ := env.Repo.ListRaw(context.Background(), repo.RawFilter{Limit: 100})
	if len(rows) != 0 {
		t.Fatalf("valid row in failed batch was persisted: %d", len(rows))
	}
}

// Workstation must belong to the employee in the payload.
func TestWorkstationMismatch(t *testing.T) {
	env := NewEnv(t)
	_, err := env.Ingest.Ingest(context.Background(), []repo.SnapshotIn{
		snap(1001, 102, "2026-03-10T14:00:00Z", "VS Code", 5),
	})
	if err == nil || !isValidation(err) {
		t.Fatalf("want workstation-mismatch validation error, got %v", err)
	}
}
