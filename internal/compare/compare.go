package compare

import (
	"fmt"

	"sitevitals/internal/models"
)

// Diff computes the current-baseline delta for two successful runs of the
// same normalized URL + viewport. Missing metric values on either side are
// flagged explicitly — never coerced to zero.
func Diff(baseline, current *models.Run) models.MetricDiff {
	return models.MetricDiff{
		NavDurationMS:   delta(baseline.NavDurationMS, current.NavDurationMS),
		FCPMS:           delta(baseline.FCPMS, current.FCPMS),
		LCPMS:           delta(baseline.LCPMS, current.LCPMS),
		CLS:             delta(baseline.CLS, current.CLS),
		LongTaskCount:   deltaInt(baseline.LongTaskCount, current.LongTaskCount),
		LongTaskTotalMS: delta(baseline.LongTaskTotalMS, current.LongTaskTotalMS),
	}
}

func delta(b, c *float64) models.Delta {
	d := models.Delta{Baseline: b, Current: c}
	if b == nil {
		d.BaselineMissing = true
	}
	if c == nil {
		d.CurrentMissing = true
	}
	if b != nil && c != nil {
		change := *c - *b
		d.Change = &change
		if *b != 0 {
			pct := change / *b * 100
			d.PercentChange = &pct
		}
		// All these metrics are "smaller is better", including CLS.
		d.Regressed = change > 0
	}
	return d
}

// deltaInt compares integer metrics (long task count).
func deltaInt(b, c *int) models.Delta {
	d := models.Delta{}
	if b == nil {
		d.BaselineMissing = true
	} else {
		v := float64(*b)
		d.Baseline = &v
	}
	if c == nil {
		d.CurrentMissing = true
	} else {
		v := float64(*c)
		d.Current = &v
	}
	if b != nil && c != nil {
		change := float64(*c - *b)
		d.Change = &change
		if *b != 0 {
			pct := change / float64(*b) * 100
			d.PercentChange = &pct
		}
		d.Regressed = change > 0
	}
	return d
}

// ValidatePair ensures two runs can be compared: both successful, same
// normalized URL, same viewport, distinct runs.
func ValidatePair(baseline, current *models.Run) error {
	if baseline.ID == current.ID {
		return fmt.Errorf("cannot compare run %d with itself", current.ID)
	}
	if baseline.Status != models.StateSucceeded || current.Status != models.StateSucceeded {
		return fmt.Errorf("both runs must be succeeded (baseline=%s current=%s)", baseline.Status, current.Status)
	}
	if baseline.URL != current.URL {
		return fmt.Errorf("URL mismatch: %q vs %q", baseline.URL, current.URL)
	}
	if baseline.Viewport != current.Viewport {
		return fmt.Errorf("viewport mismatch: %q vs %q", baseline.Viewport, current.Viewport)
	}
	return nil
}
