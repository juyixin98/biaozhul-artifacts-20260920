package collector

import (
	"encoding/json"
	"testing"

	"sitevitals/internal/models"
)

func TestBuildMetricsAllCollected(t *testing.T) {
	fcp, lcp := 120.0, 300.0
	d := &extractedData{
		LCPSupported: true, CLSSupported: true, LTSupported: true,
		CLS: 0.05, Now: 1234,
		Nav: &struct {
			TTFB         float64 `json:"ttfb"`
			DCL          float64 `json:"dcl"`
			Load         float64 `json:"load"`
			DomComplete  float64 `json:"domComplete"`
			TransferSize float64 `json:"transferSize"`
		}{TTFB: 40, DCL: 200, Load: 250, DomComplete: 240},
		FCP: &fcp,
		LCP: &struct {
			Value      float64 `json:"value"`
			RenderTime float64 `json:"renderTime"`
			Element    string  `json:"element"`
			URL        string  `json:"url"`
		}{Value: lcp},
		LongTasks: []struct {
			Start float64 `json:"start"`
			Dur   float64 `json:"dur"`
		}{{Start: 100, Dur: 120}, {Start: 500, Dur: 60}},
	}
	ms := buildMetrics(d)
	byName := map[string]*models.Metric{}
	for _, m := range ms {
		byName[m.Name] = m
		if m.Name != MetricLongTasks && m.Status != models.MetricCollected {
			t.Errorf("metric %s status=%s want collected", m.Name, m.Status)
		}
	}
	if byName[MetricFCP].ValueMS == nil || *byName[MetricFCP].ValueMS != 120 {
		t.Errorf("fcp wrong: %+v", byName[MetricFCP])
	}
	if byName[MetricLCP].ValueMS == nil || *byName[MetricLCP].ValueMS != 300 {
		t.Errorf("lcp wrong: %+v", byName[MetricLCP])
	}
	if byName[MetricCLS].ValueCLS == nil || *byName[MetricCLS].ValueCLS != 0.05 {
		t.Errorf("cls wrong: %+v", byName[MetricCLS])
	}
	var w longTaskWindow
	if err := json.Unmarshal([]byte(byName[MetricLongTasks].Detail), &w); err != nil {
		t.Fatal(err)
	}
	if w.Count != 2 || w.ThresholdMS != 50 || w.MaxDurationMS != 120 || w.TotalDurationMS != 180 {
		t.Fatalf("long task summary wrong: %+v", w)
	}
	if w.WindowEndMS != 1234 {
		t.Errorf("window end=%v want 1234 (window explicitly recorded)", w.WindowEndMS)
	}
}

// TestBuildMetricsMissingNeverZero verifies unsupported/unavailable metrics are
// marked explicitly and carry no value — zero must never masquerade as data.
func TestBuildMetricsMissingNeverZero(t *testing.T) {
	d := &extractedData{
		LCPSupported: false, // browser does not support LCP
		CLSSupported: false,
		LTSupported:  false,
		Nav:          nil, // no NavigationTiming entry
		FCP:          nil, // no paint entry
		LCP:          nil,
	}
	ms := buildMetrics(d)
	for _, m := range ms {
		if m.Status == models.MetricCollected {
			t.Fatalf("metric %s unexpectedly collected", m.Name)
		}
		if m.ValueMS != nil {
			t.Fatalf("metric %s missing value must be NULL, got %v", m.Name, *m.ValueMS)
		}
		if m.ValueCLS != nil {
			t.Fatalf("metric %s missing cls must be NULL, got %v", m.Name, *m.ValueCLS)
		}
		if m.Detail == "" {
			t.Fatalf("metric %s missing must explain why in detail", m.Name)
		}
	}
}

// TestBuildMetricsLCPObserverButNoEntry: supported observer that never fired
// must be "failed" (collection gap), not silently zero.
func TestBuildMetricsLCPObserverButNoEntry(t *testing.T) {
	d := &extractedData{LCPSupported: true, CLSSupported: true, LTSupported: true}
	ms := buildMetrics(d)
	byName := map[string]*models.Metric{}
	for _, m := range ms {
		byName[m.Name] = m
	}
	if byName[MetricLCP].Status != models.MetricFailed {
		t.Fatalf("lcp status=%s want failed", byName[MetricLCP].Status)
	}
	if byName[MetricLCP].ValueMS != nil {
		t.Fatal("failed LCP must not carry a value")
	}
}
