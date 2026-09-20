package policy

import (
	"testing"
	"time"
)

func testPolicy() Policy {
	return New(1, 9*60, 18*60,
		[]int64{1, 2, 3, 4, 5}, // Mon-Fri
		[]string{"1Password"},  // excluded apps
		[]int64{3})             // exempt department
}

func at(loc *time.Location, y int, m time.Month, d, hh, mm int) time.Time {
	return time.Date(y, m, d, hh, mm, 0, 0, loc).UTC()
}

func TestMonitoringWindow(t *testing.T) {
	p := testPolicy()
	sh, _ := time.LoadLocation("Asia/Shanghai")
	// 2026-09-14 is a Monday.
	if !p.Allows(1, sh, at(sh, 2026, 9, 14, 9, 0), "Code") {
		t.Error("09:00 local on a workday should be allowed")
	}
	if p.Allows(1, sh, at(sh, 2026, 9, 14, 8, 59), "Code") {
		t.Error("before window should be filtered")
	}
	if p.Allows(1, sh, at(sh, 2026, 9, 14, 18, 0), "Code") {
		t.Error("window end is exclusive")
	}
	if p.Allows(1, sh, at(sh, 2026, 9, 13, 10, 0), "Code") { // Sunday
		t.Error("weekend should be filtered")
	}
}

func TestWindowIsLocalNotUTC(t *testing.T) {
	p := testPolicy()
	sh, _ := time.LoadLocation("Asia/Shanghai") // UTC+8
	// 02:00 UTC == 10:00 in Shanghai: inside the window.
	if !p.Allows(1, sh, time.Date(2026, 9, 14, 2, 0, 0, 0, time.UTC), "Code") {
		t.Error("10:00 local (02:00 UTC) should be allowed")
	}
	// 02:00 UTC for a UTC employee is outside the window.
	if p.Allows(1, time.UTC, time.Date(2026, 9, 14, 2, 0, 0, 0, time.UTC), "Code") {
		t.Error("02:00 local should be filtered")
	}
}

func TestExcludedApp(t *testing.T) {
	p := testPolicy()
	sh, _ := time.LoadLocation("Asia/Shanghai")
	if p.Allows(1, sh, at(sh, 2026, 9, 14, 10, 0), "1password") {
		t.Error("excluded app (case-insensitive) should be filtered")
	}
}

func TestExemptDepartment(t *testing.T) {
	p := testPolicy()
	sh, _ := time.LoadLocation("Asia/Shanghai")
	if p.Allows(3, sh, at(sh, 2026, 9, 14, 10, 0), "Code") {
		t.Error("exempt department should be filtered")
	}
}
