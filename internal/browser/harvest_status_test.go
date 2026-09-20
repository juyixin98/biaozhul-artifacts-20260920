package browser

import (
	"encoding/json"
	"testing"

	"sitevitals/internal/models"
)

// These cases cover metric paths that cannot be forced against a real
// Chromium (a modern browser always exposes the APIs). They pin the core
// "no zero-faking" guarantee: an unavailable metric is NULL + explicit
// status, while a genuinely measured zero (CLS 0, zero long tasks) stays 0.

func decodeHarvest(t *testing.T, payload string) *harvested {
	t.Helper()
	var h harvested
	if err := json.Unmarshal([]byte(payload), &h); err != nil {
		t.Fatal(err)
	}
	return &h
}

func TestHarvestStatuses_UnsupportedAreExplicit(t *testing.T) {
	payload := `{
	  "installError": null,
	  "fcp": null, "lcp": null, "cls": 0,
	  "fcpSupported": false, "lcpSupported": false,
	  "clsSupported": false, "ltSupported": false,
	  "longTasks": [], "bufferFull": false,
	  "nav": null, "navError": "boom",
	  "resources": [], "resourceError": ""
	}`
	d := &Diagnostics{}
	m := fillRunMetrics(decodeHarvest(t, payload), nil, d)

	if m.NavDurationMS != nil {
		t.Fatalf("missing nav timing must be NULL, got %v", *m.NavDurationMS)
	}
	if m.FCPMS != nil || m.MetricStatus.FCP != models.MetricUnsupported {
		t.Fatalf("FCP must be NULL/unsupported, got %v %s", m.FCPMS, m.MetricStatus.FCP)
	}
	if m.LCPMS != nil || m.MetricStatus.LCP != models.MetricUnsupported {
		t.Fatalf("LCP must be NULL/unsupported, got %v %s", m.LCPMS, m.MetricStatus.LCP)
	}
	if m.CLS != nil || m.MetricStatus.CLS != models.MetricUnsupported {
		t.Fatalf("CLS must be NULL/unsupported, got %v %s", m.CLS, m.MetricStatus.CLS)
	}
	if m.LongTaskCount != nil || m.MetricStatus.LongTasks != models.MetricUnsupported {
		t.Fatal("long tasks must be NULL/unsupported")
	}
	if m.MetricStatus.Navigation != models.MetricFailed {
		t.Fatalf("nav with error should be failed, got %s", m.MetricStatus.Navigation)
	}
}

func TestHarvestStatuses_SupportedButNoValue(t *testing.T) {
	payload := `{
	  "fcp": null, "lcp": null, "cls": 0,
	  "fcpSupported": true, "lcpSupported": true,
	  "clsSupported": true, "ltSupported": true,
	  "longTasks": [],
	  "nav": {"startTime":0,"domContentLoaded":10,"loadEventEnd":100,"responseEnd":90,"duration":100,"transferSize":500,"timingRestricted":false},
	  "resources": []
	}`
	d := &Diagnostics{}
	m := fillRunMetrics(decodeHarvest(t, payload), nil, d)

	if m.FCPMS != nil || m.MetricStatus.FCP != models.MetricFailed {
		t.Fatalf("FCP supported-but-absent must be NULL/failed: %v %s", m.FCPMS, m.MetricStatus.FCP)
	}
	if m.LCPMS != nil || m.MetricStatus.LCP != models.MetricFailed {
		t.Fatalf("LCP supported-but-absent must be NULL/failed: %v %s", m.LCPMS, m.MetricStatus.LCP)
	}
	// CLS 0 is a real measurement, not an absence.
	if m.CLS == nil || *m.CLS != 0 || m.MetricStatus.CLS != models.MetricOK {
		t.Fatalf("CLS=0 with support must be stored as measured 0, got %v %s", m.CLS, m.MetricStatus.CLS)
	}
	if m.LongTaskCount == nil || *m.LongTaskCount != 0 || m.MetricStatus.LongTasks != models.MetricOK {
		t.Fatal("zero observed long tasks is a valid count 0, not missing")
	}
	if m.NavDurationMS == nil || *m.NavDurationMS != 100 {
		t.Fatalf("nav duration should come from loadEventEnd, got %v", m.NavDurationMS)
	}
}
