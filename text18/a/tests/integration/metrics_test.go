package integration

import (
	"context"
	"testing"
	"time"

	"sircc/internal/service"
)

// Durations use only recorded phase timestamps; while an interval's ending
// phase is not entered the metric is null (never faked against now()).
func TestMetrics_OnlyValidPhaseTimes(t *testing.T) {
	f := newFixture(t)
	id := f.createP2Incident(t)
	f.assignResponder(t, id, f.responder1().ID)

	d0 := f.get(t, id)
	if d0.Metrics == nil {
		t.Fatalf("missing metrics")
	}
	if d0.Metrics.TimeToContainmentSeconds != nil {
		t.Fatalf("containment metric must be null before containment, got %v",
			*d0.Metrics.TimeToContainmentSeconds)
	}
	if d0.Metrics.TimeToResolutionSeconds != nil || d0.Metrics.TimeToClosureSeconds != nil {
		t.Fatalf("resolution/closure metrics must be null at detection")
	}

	f.mustTransition(t, f.analyst1(), id, "triage", 1)
	f.clk.Advance(2 * time.Hour)
	f.mustTransition(t, f.responder1(), id, "contain", 2)

	d1 := f.get(t, id)
	if d1.Metrics.TimeToContainmentSeconds == nil {
		t.Fatalf("containment metric should be set")
	}
	// Detection was entered at clock T0 using the mock; triage at T0 too
	// (clock advanced only after triage), containment at T0+2h.
	if got := *d1.Metrics.TimeToContainmentSeconds; got < 7190 || got > 7210 {
		t.Fatalf("containment duration ~7200s expected, got %v", got)
	}
	if d1.Metrics.TimeToResolutionSeconds != nil {
		t.Fatalf("resolution must still be null (recovery not entered)")
	}
	// The containment phase itself has no end yet (eradication not entered).
	if d1.Metrics.ContainmentPhaseSeconds != nil {
		t.Fatalf("open containment phase must not be assigned a fake end")
	}

	f.clk.Advance(30 * time.Minute)
	f.mustTransition(t, f.responder1(), id, "eradicate", 3)
	d2 := f.get(t, id)
	if d2.Metrics.ContainmentPhaseSeconds == nil {
		t.Fatalf("containment phase duration should now be known")
	}
	if got := *d2.Metrics.ContainmentPhaseSeconds; got < 1790 || got > 1810 {
		t.Fatalf("containment phase ~1800s expected, got %v", got)
	}
}

// Export contains the ordered phase records and an evidence summary, while
// respecting case scope.
func TestExport_ContentsAndScope(t *testing.T) {
	f := newFixture(t)
	id := f.createP2Incident(t)
	f.assignResponder(t, id, f.responder1().ID)
	f.mustTransition(t, f.analyst1(), id, "triage", 1)

	if _, err := f.svc.AddEvidence(context.Background(), f.analyst1(), id,
		service.AddEvidenceInput{Content: "exportable finding"}); err != nil {
		t.Fatalf("evidence: %v", err)
	}

	report, err := f.svc.Export(context.Background(), f.analyst1(), id)
	if err != nil {
		t.Fatalf("export: %v", err)
	}
	if len(report.Phases) != 2 {
		t.Fatalf("want 2 phase records, got %d", len(report.Phases))
	}
	if report.Phases[0].Phase != "detection" || report.Phases[1].Phase != "triage" {
		t.Fatalf("phase order wrong: %+v", report.Phases)
	}
	if len(report.Evidence) != 1 || report.Evidence[0].Preview != "exportable finding" {
		t.Fatalf("evidence summary wrong: %+v", report.Evidence)
	}
	if report.Metrics == nil {
		t.Fatalf("metrics missing in export")
	}

	// Outsider cannot export.
	if _, err := f.svc.Export(context.Background(), f.analyst2(), id); err == nil {
		t.Fatalf("outsider export should fail")
	}
}
