package store

import "sitevitals/internal/models"

// BudgetDefaults are the global alert thresholds seeded on first start.
// They follow common web-vitals guidance; operators can tune them via
// PUT /api/budgets/global.
var BudgetDefaults = models.Budget{
	SiteID:          0,
	FCPMS:           floatPtr(1800),
	LCPMS:           floatPtr(2500),
	CLS:             floatPtr(0.1),
	NavDurationMS:   floatPtr(3000),
	LongTaskTotalMS: floatPtr(1000),
}

func floatPtr(v float64) *float64 { return &v }

// mergeBudget overlays non-nil override thresholds on top of base.
func mergeBudget(base, override *models.Budget) *models.Budget {
	out := *base
	out.ID = override.ID
	out.SiteID = override.SiteID
	if override.FCPMS != nil {
		out.FCPMS = override.FCPMS
	}
	if override.LCPMS != nil {
		out.LCPMS = override.LCPMS
	}
	if override.CLS != nil {
		out.CLS = override.CLS
	}
	if override.NavDurationMS != nil {
		out.NavDurationMS = override.NavDurationMS
	}
	if override.LongTaskTotalMS != nil {
		out.LongTaskTotalMS = override.LongTaskTotalMS
	}
	return &out
}
