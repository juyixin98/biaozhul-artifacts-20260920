package policy

import (
	"testing"
	"time"

	"desklens/internal/repo"
)

func TestExclusionsAndExemptions(t *testing.T) {
	f, err := NewFilter(repo.Policy{
		Version:           1,
		WindowStartMinute: 9 * 60,
		WindowEndMinute:   18 * 60,
		ExcludedPatterns:  []string{"1password*", "*keychain*"},
		ExemptDepartments: []int64{3},
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, app := range []string{"1Password", "1PASSWORD 8", "macOS Keychain"} {
		if !f.ExcludedApp(app) {
			t.Errorf("ExcludedApp(%q) = false", app)
		}
	}
	if f.ExcludedApp("VS Code") {
		t.Error("VS Code incorrectly excluded")
	}
	if !f.ExemptDepartment(3) || f.ExemptDepartment(1) {
		t.Error("exempt department map wrong")
	}
}

func TestWindowIntervals(t *testing.T) {
	ny, _ := time.LoadLocation("America/New_York")
	parse := func(s string) time.Time {
		ts, _ := time.Parse(time.RFC3339, s)
		return ts
	}

	f, _ := NewFilter(repo.Policy{WindowStartMinute: 9 * 60, WindowEndMinute: 18 * 60})
	if !f.InWindow(parse("2026-03-10T14:00:00Z"), ny) { // 10:00 NY
		t.Error("10:00 NY should be in window")
	}
	if f.InWindow(parse("2026-03-10T03:00:00Z"), ny) { // 22:00 NY previous day
		t.Error("22:00 NY should be out of window")
	}

	// Whole-day window expressed as start == end.
	all, _ := NewFilter(repo.Policy{WindowStartMinute: 0, WindowEndMinute: 0})
	if !all.InWindow(parse("2026-03-10T03:00:00Z"), ny) {
		t.Error("start==end should mean a 24h window")
	}

	// Overnight window 22:00-06:00.
	night, _ := NewFilter(repo.Policy{WindowStartMinute: 22 * 60, WindowEndMinute: 6 * 60})
	if !night.InWindow(parse("2026-03-10T04:00:00Z"), ny) { // 00:00 NY
		t.Error("midnight should be inside overnight window")
	}
	if night.InWindow(parse("2026-03-10T16:00:00Z"), ny) { // 12:00 NY
		t.Error("noon should be outside overnight window")
	}
}
