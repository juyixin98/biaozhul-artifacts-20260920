package integrationtest

import (
	"context"
	"testing"

	"desklens/internal/repo"
)

func mustDaily(t *testing.T, env *Env, employeeID int64, day string) repo.DailySummary {
	t.Helper()
	rows, err := env.Repo.ListDaily(context.Background(), repo.SummaryFilter{
		EmployeeID: &employeeID, Limit: 10,
	})
	if err != nil {
		t.Fatal(err)
	}
	d := date(day)
	for _, r := range rows {
		if r.LocalDate.Equal(d) {
			return r
		}
	}
	t.Fatalf("daily summary for %d/%s not found", employeeID, day)
	return repo.DailySummary{}
}

// A late snapshot for an old day only recomputes that day and its week, and
// the totals match what a full rebuild from raw data produces.
func TestLateSnapshotAndRebuildConsistency(t *testing.T) {
	env := NewEnv(t)
	ctx := context.Background()

	// Day 1: two VS Code minutes for Ada.
	if _, err := env.Ingest.Ingest(ctx, []repo.SnapshotIn{
		snap(1001, 101, "2026-03-10T10:00:00Z", "VS Code", 40),
		snap(1001, 101, "2026-03-10T10:01:00Z", "VS Code", 30),
	}); err != nil {
		t.Fatal(err)
	}
	before := mustDaily(t, env, 101, "2026-03-10")
	if before.ProductiveCount != 70 || before.ActiveMinutes != 2 {
		t.Fatalf("initial daily = %+v", before)
	}

	// A different day's traffic must not touch day 1.
	if _, err := env.Ingest.Ingest(ctx, []repo.SnapshotIn{
		snap(1001, 101, "2026-03-11T10:00:00Z", "Slack", 12),
	}); err != nil {
		t.Fatal(err)
	}

	// Late upload for day 1 (arrives "days later").
	if _, err := env.Ingest.Ingest(ctx, []repo.SnapshotIn{
		snap(1001, 101, "2026-03-10T10:02:00Z", "VS Code", 25),
	}); err != nil {
		t.Fatal(err)
	}
	afterLate := mustDaily(t, env, 101, "2026-03-10")
	if afterLate.ProductiveCount != 95 || afterLate.ActiveMinutes != 3 {
		t.Fatalf("after late snapshot daily = %+v, want productive 95 / 3 minutes", afterLate)
	}
	day2 := mustDaily(t, env, 101, "2026-03-11")
	if day2.NonProductiveCount != 12 {
		t.Fatalf("day 2 changed unexpectedly: %+v", day2)
	}

	// Mutate the summary out-of-band to prove the rebuild actually derives
	// from raw rows rather than trusting the stored value.
	if _, err := env.DB.Exec(
		`UPDATE employee_daily_summary SET productive_count = 1
		  WHERE employee_id = 101 AND local_date = DATE '2026-03-10'`); err != nil {
		t.Fatal(err)
	}

	report, err := env.Agg.RebuildAll(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if report.DaysProcessed < 2 {
		t.Fatalf("rebuild report = %+v", report)
	}
	rebuilt := mustDaily(t, env, 101, "2026-03-10")
	if rebuilt.ProductiveCount != 95 || rebuilt.ActiveMinutes != 3 {
		t.Fatalf("rebuilt daily = %+v, want identical to incremental (95/3)", rebuilt)
	}

	// Weekly rebuild must agree with the incremental weekly total.
	week := date("2026-03-09")
	var weekly repo.WeeklySummary
	if err := env.DB.Get(&weekly,
		`SELECT * FROM department_weekly_summary WHERE department_id = 1 AND iso_week = $1`,
		week); err != nil {
		t.Fatal(err)
	}
	if weekly.ProductiveCount != 95 || weekly.NonProductiveCount != 12 ||
		weekly.ActiveEmployeeDays != 2 {
		t.Fatalf("weekly = %+v, want productive 95, non-prod 12, 2 employee-days", weekly)
	}
}

// Classification versions: publishing new rules only affects new ingestion;
// every raw row and summary keeps the version it was stamped with, and
// rebuilding cannot silently recolor history.
func TestClassificationVersioning(t *testing.T) {
	env := NewEnv(t)
	ctx := context.Background()

	if _, err := env.Ingest.Ingest(ctx, []repo.SnapshotIn{
		snap(1001, 101, "2026-03-10T10:00:00Z", "VS Code", 40),
	}); err != nil {
		t.Fatal(err)
	}

	// Publish v2 where VS Code becomes non-productive (catch-all ordering
	// keeps a specific rule ahead of '*').
	v, err := env.Repo.PublishClassification(ctx, repo.NewClassification{Rules: []repo.ClassificationRule{
		{RuleID: 9, Pattern: "vs code", Category: "non_productive", Priority: 200},
		{RuleID: 1, Pattern: "*", Category: "neutral", Priority: 0},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if v != 2 {
		t.Fatalf("new classif version = %d, want 2", v)
	}

	// Historical row keeps v1 semantics + stamp after a rebuild.
	if err := env.Agg.RebuildDaily(ctx, 101, date("2026-03-10")); err != nil {
		t.Fatal(err)
	}
	hist := mustDaily(t, env, 101, "2026-03-10")
	if hist.ClassifVersion != 1 || hist.ProductiveCount != 40 {
		t.Fatalf("history recolored by rule change: %+v", hist)
	}

	// New ingestion uses v2.
	if _, err := env.Ingest.Ingest(ctx, []repo.SnapshotIn{
		snap(1001, 101, "2026-03-11T10:00:00Z", "VS Code", 7),
	}); err != nil {
		t.Fatal(err)
	}
	day2 := mustDaily(t, env, 101, "2026-03-11")
	if day2.ClassifVersion != 2 || day2.NonProductiveCount != 7 {
		t.Fatalf("new ingestion did not apply v2: %+v", day2)
	}
}

// Stable tie-break: equal-priority rules match the lowest rule_id first.
func TestRuleTieBreak(t *testing.T) {
	env := NewEnv(t)
	ctx := context.Background()
	if _, err := env.Repo.PublishClassification(ctx, repo.NewClassification{Rules: []repo.ClassificationRule{
		{RuleID: 100, Pattern: "Fire*", Category: "productive", Priority: 50},
		{RuleID: 101, Pattern: "Fire*", Category: "non_productive", Priority: 50},
		{RuleID: 102, Pattern: "*", Category: "neutral", Priority: 0},
	}}); err != nil {
		t.Fatal(err)
	}
	if _, err := env.Ingest.Ingest(ctx, []repo.SnapshotIn{
		snap(1001, 101, "2026-03-10T10:00:00Z", "Firefox", 5),
	}); err != nil {
		t.Fatal(err)
	}
	d := mustDaily(t, env, 101, "2026-03-10")
	if d.ProductiveCount != 5 {
		t.Fatalf("tie-break not by rule_id: %+v", d)
	}
}

// Policy versions only affect new ingestion: exempting a department after
// publication does not remove already-collected rows.
func TestPolicyVersionOnlyAffectsNewIngestion(t *testing.T) {
	env := NewEnv(t)
	ctx := context.Background()
	if _, err := env.Ingest.Ingest(ctx, []repo.SnapshotIn{
		snap(1003, 103, "2026-03-10T18:00:00Z", "Zoom Meetings", 30),
	}); err != nil {
		t.Fatal(err)
	}
	// v2 exempts department 2 (Sales).
	if _, err := env.Repo.PublishPolicy(ctx, repo.NewPolicy{
		WindowStartMinute: 540, WindowEndMinute: 1080,
		ExcludedPatterns: []string{"1password*"}, ExemptDepartments: []int64{2},
	}); err != nil {
		t.Fatal(err)
	}
	out, err := env.Ingest.Ingest(ctx, []repo.SnapshotIn{
		snap(1003, 103, "2026-03-10T18:01:00Z", "Zoom Meetings", 11),
	})
	if err != nil {
		t.Fatal(err)
	}
	if out.PolicyVersion != 2 || out.Filtered.ExemptDepartment != 1 || out.Accepted != 0 {
		t.Fatalf("new policy not applied: %+v", out)
	}
	d := mustDaily(t, env, 103, "2026-03-10")
	if d.PolicyVersion != 1 || d.NonProductiveCount != 30 {
		t.Fatalf("old data changed by new policy version: %+v", d)
	}
}
