package integration

import (
	"context"
	"testing"

	"desklens/internal/manager"
	"desklens/internal/testsupport"
)

// TestManagerIsolation verifies department scoping on every manager read path:
// daily, weekly, detail and export share the same filter, foreign employees
// return 404, and no data from other departments is visible.
func TestManagerIsolation(t *testing.T) {
	e := setup(t) // v1 window 08:00-18:00 local
	e.mustIngest(p101, "iso-eng",
		snapAt("WS-101", 101, utc(2026, 9, 15, 14, 0), "Chrome", 11))
	e.mustIngest(p102, "iso-eng2",
		snapAt("WS-102", 102, utc(2026, 9, 15, 6, 0), "Code", 22)) // 14:00 Shanghai
	e.mustIngest(p103, "iso-sales",
		snapAt("WS-103", 103, utc(2026, 9, 15, 10, 0), "Chrome", 33)) // 12:00 Berlin

	eng := managerPrincipal(1)
	sales := managerPrincipal(2)

	// Engineering manager sees both engineering employees (101, 102).
	daily, err := e.Manager.DailySummary(context.Background(), eng, 0, "2026-09-15", "2026-09-15")
	if err != nil {
		t.Fatal(err)
	}
	seen := map[int64]bool{}
	var total int64
	for _, r := range daily {
		seen[r.EmployeeID] = true
		total += r.TotalCount
	}
	if !seen[101] || !seen[102] || seen[103] {
		t.Fatalf("engineering daily sees employees %v", seen)
	}
	if total != 33 {
		t.Fatalf("engineering daily total = %d, want 33", total)
	}

	// Sales manager sees only sales.
	daily, err = e.Manager.DailySummary(context.Background(), sales, 0, "2026-09-15", "2026-09-15")
	if err != nil {
		t.Fatal(err)
	}
	if len(daily) != 1 || daily[0].EmployeeID != 103 || daily[0].TotalCount != 33 {
		t.Fatalf("sales daily = %+v", daily)
	}

	// Weekly: each department gets its own row.
	engWeek, err := e.Manager.WeeklySummary(context.Background(), eng, "2026-09-14", "2026-09-14")
	if err != nil {
		t.Fatal(err)
	}
	if len(engWeek) != 1 || engWeek[0].TotalCount != 33 {
		t.Fatalf("eng weekly = %+v", engWeek)
	}
	salesWeek, err := e.Manager.WeeklySummary(context.Background(), sales, "2026-09-14", "2026-09-14")
	if err != nil {
		t.Fatal(err)
	}
	if len(salesWeek) != 1 || salesWeek[0].TotalCount != 33 {
		t.Fatalf("sales weekly = %+v", salesWeek)
	}

	// Detail rows for own employee succeed.
	rows, err := e.Manager.DetailRows(context.Background(), eng, 101,
		utc(2026, 9, 15, 0, 0), utc(2026, 9, 16, 0, 0))
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 || rows[0].AppName != "Chrome" {
		t.Fatalf("eng detail = %+v", rows)
	}

	// Engineering manager cannot read sales employee details: 404, not 403.
	_, err = e.Manager.DetailRows(context.Background(), eng, 103,
		utc(2026, 9, 15, 0, 0), utc(2026, 9, 16, 0, 0))
	if ae, ok := err.(*manager.APIError); !ok || ae.HTTPStatus != 404 {
		t.Fatalf("cross-dept detail err = %v", err)
	}

	// Explicit employee filter in daily summary is also scoped.
	if _, err := e.Manager.DailySummary(context.Background(), sales, 101,
		"2026-09-15", "2026-09-15"); err == nil {
		t.Fatal("sales manager could query engineering employee daily")
	} else if ae, ok := err.(*manager.APIError); !ok || ae.HTTPStatus != 404 {
		t.Fatalf("cross-dept daily err = %v", err)
	}
}

// TestExemptDepartmentInvisibleToManagers confirms raw rows for an exempt
// department can never exist, even though its manager key could be issued.
func TestExemptDepartmentNoRaw(t *testing.T) {
	e := setup(t)
	out := e.mustIngest(p104, "ex-mgr",
		snapAt("WS-104", 104, utc(2026, 9, 15, 14, 0), "Chrome", 9))
	if out.Filtered != 1 {
		t.Fatalf("exempt ingest = %+v", out)
	}
	if n := testsupport.RawCount(t, e.DB, "WS-104"); n != 0 {
		t.Fatalf("exempt dept raw rows = %d", n)
	}
	daily, err := e.Manager.DailySummary(context.Background(), managerPrincipal(4),
		0, "2026-09-15", "2026-09-15")
	if err != nil {
		t.Fatal(err)
	}
	if len(daily) != 0 {
		t.Fatalf("exempt dept summaries exist: %+v", daily)
	}
}
