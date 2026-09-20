package compare_test

import (
	"testing"

	"sitevitals/internal/compare"
	"sitevitals/internal/models"
)

func f(v float64) *float64 { return &v }
func i(v int) *int         { return &v }

func TestDiff_ValuesAndRegression(t *testing.T) {
	base := &models.Run{
		ID: 1, Status: models.StateSucceeded,
		URL: "http://demo.local/p", Viewport: models.ViewportDesktop,
		FCPMS: f(100), LCPMS: f(200), CLS: f(0.01), NavDurationMS: f(300),
		LongTaskCount: i(1), LongTaskTotalMS: f(60),
	}
	cur := &models.Run{
		ID: 2, Status: models.StateSucceeded,
		URL: "http://demo.local/p", Viewport: models.ViewportDesktop,
		FCPMS: f(150), LCPMS: f(180), CLS: f(0.02), NavDurationMS: f(330),
		LongTaskCount: i(3), LongTaskTotalMS: f(140),
	}
	if err := compare.ValidatePair(base, cur); err != nil {
		t.Fatal(err)
	}
	d := compare.Diff(base, cur)
	if *d.FCPMS.Change != 50 || *d.FCPMS.PercentChange != 50 || !d.FCPMS.Regressed {
		t.Fatalf("fcp diff wrong: %+v", d.FCPMS)
	}
	if *d.LCPMS.Change != -20 || d.LCPMS.Regressed {
		t.Fatalf("lcp improvement mislabeled: %+v", d.LCPMS)
	}
	if !d.CLS.Regressed || *d.CLS.Change != 0.01 {
		t.Fatalf("cls diff wrong: %+v", d.CLS)
	}
	if *d.LongTaskCount.Change != 2 || !d.LongTaskCount.Regressed {
		t.Fatalf("long task count diff wrong: %+v", d.LongTaskCount)
	}
}

func TestDiff_MissingMetricsAreExplicit(t *testing.T) {
	base := &models.Run{ID: 1, Status: models.StateSucceeded, URL: "u", Viewport: models.ViewportMobile}
	cur := &models.Run{ID: 2, Status: models.StateSucceeded, URL: "u", Viewport: models.ViewportMobile, FCPMS: f(100)}
	if err := compare.ValidatePair(base, cur); err != nil {
		t.Fatal(err)
	}
	d := compare.Diff(base, cur)
	if !d.FCPMS.BaselineMissing || d.FCPMS.CurrentMissing || d.FCPMS.Change != nil {
		t.Fatalf("missing baseline must not produce a delta: %+v", d.FCPMS)
	}
	if !d.LCPMS.BaselineMissing || !d.LCPMS.CurrentMissing {
		t.Fatal("both-missing must be flagged")
	}
}

func TestValidatePair_Rules(t *testing.T) {
	ok := func(id uint, url string, vp models.Viewport, status string) *models.Run {
		return &models.Run{ID: id, Status: status, URL: url, Viewport: vp}
	}
	if err := compare.ValidatePair(ok(1, "u", "desktop", "succeeded"), ok(1, "u", "desktop", "succeeded")); err == nil {
		t.Fatal("self-compare must fail")
	}
	if err := compare.ValidatePair(ok(1, "u", "desktop", "failed"), ok(2, "u", "desktop", "succeeded")); err == nil {
		t.Fatal("failed run must not be comparable")
	}
	if err := compare.ValidatePair(ok(1, "u1", "desktop", "succeeded"), ok(2, "u2", "desktop", "succeeded")); err == nil {
		t.Fatal("different URLs must fail")
	}
	if err := compare.ValidatePair(ok(1, "u", "desktop", "succeeded"), ok(2, "u", "mobile", "succeeded")); err == nil {
		t.Fatal("different viewports must fail")
	}
}
