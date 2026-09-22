package detection_test

import (
	"context"
	"testing"
	"time"

	"anomalywatch/internal/models"
)

// After an employee's time zone changes, a night window that contained night
// activity under the old zone but not the new one must be reconciled: its open
// alert is withdrawn on the next sweep, while a window that is still nightly
// keeps its alert. Resolved alerts are preserved.
func TestTimezoneChangeReconcilesNightAlerts(t *testing.T) {
	e := setup(t)
	alice := e.emps["Alice Chen"] // Asia/Shanghai

	// 2026-09-20 13:00Z = 21:00 Shanghai (night); 05:00Z same day = 13:00
	// Shanghai (daytime). Under UTC, 13:00Z is daytime and 05:00Z is night.
	nightOnlyInShanghai := time.Date(2026, 9, 20, 13, 0, 0, 0, time.UTC)
	nightOnlyInUTC := time.Date(2026, 9, 20, 5, 0, 0, 0, time.UTC)
	insertEvent(t, e.db, "s", "tz-ev-1", alice, models.EventTypeLogin, nightOnlyInShanghai, nil)
	insertEvent(t, e.db, "s", "tz-ev-2", alice, models.EventTypeLogin, nightOnlyInUTC, nil)

	processAll(t, e)
	if err := e.eng.RunSweep(context.Background()); err != nil {
		t.Fatal(err)
	}
	// Under Shanghai: exactly one night alert (owned 2026-09-20).
	if got := alertCount(t, e.db, models.RuleNightActivity, alice); got != 1 {
		t.Fatalf("night alerts under Shanghai = %d, want 1", got)
	}

	// Admin moves Alice to UTC; event-driven evaluations for night/stat are
	// invalidated exactly like the admin service does.
	if err := e.db.Model(&models.Employee{}).Where("id = ?", alice).
		Update("time_zone", "UTC").Error; err != nil {
		t.Fatal(err)
	}
	if err := e.db.Where("employee_id = ? AND window_scope IN ?",
		alice, []string{models.ScopeNight, models.ScopeStat}).
		Delete(&models.WindowEvaluation{}).Error; err != nil {
		t.Fatal(err)
	}

	if err := e.eng.RunSweep(context.Background()); err != nil {
		t.Fatal(err)
	}
	// Under UTC the 13:00Z event is daytime: the old alert must be withdrawn.
	// 05:00Z becomes night (owned 2026-09-19), replacing it — still exactly one.
	if got := alertCount(t, e.db, models.RuleNightActivity, alice); got != 1 {
		t.Fatalf("night alerts after move to UTC = %d, want 1 (reconciled)", got)
	}
	var a models.Alert
	if err := e.db.Where("rule_code = ? AND employee_id = ?", models.RuleNightActivity, alice).First(&a).Error; err != nil {
		t.Fatal(err)
	}
	if a.WindowStart.UTC().Format("2006-01-02T15") != "2026-09-19T20" {
		t.Fatalf("remaining night window starts %s, want 2026-09-19 20:00Z (UTC night)", a.WindowStart)
	}
}

// Withdraw must not touch terminal alerts even when their window disappears.
func TestTimezoneChangeKeepsResolvedAlerts(t *testing.T) {
	e := setup(t)
	alice := e.emps["Alice Chen"]
	insertEvent(t, e.db, "s", "tz-res", alice, models.EventTypeLogin,
		time.Date(2026, 9, 20, 13, 0, 0, 0, time.UTC), nil)
	processAll(t, e)
	if err := e.eng.RunSweep(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := e.db.Model(&models.Alert{}).
		Where("employee_id = ? AND rule_code = ?", alice, models.RuleNightActivity).
		Updates(map[string]any{
			"status":      models.AlertStatusResolved,
			"resolved_at": time.Now().UTC(),
		}).Error; err != nil {
		t.Fatal(err)
	}

	if err := e.db.Model(&models.Employee{}).Where("id = ?", alice).Update("time_zone", "UTC").Error; err != nil {
		t.Fatal(err)
	}
	if err := e.db.Where("employee_id = ? AND window_scope IN ?",
		alice, []string{models.ScopeNight, models.ScopeStat}).
		Delete(&models.WindowEvaluation{}).Error; err != nil {
		t.Fatal(err)
	}
	if err := e.eng.RunSweep(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := alertCount(t, e.db, models.RuleNightActivity, alice); got != 1 {
		t.Fatalf("resolved night alerts after zone change = %d, want preserved 1", got)
	}
}
