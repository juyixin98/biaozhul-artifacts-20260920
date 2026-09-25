package fakesvc

import (
	"errors"
	"testing"
	"time"

	"conditionupdate/internal/clock"
)

func TestRecordAndEntries(t *testing.T) {
	a := NewAudit(clock.NewFake(time.Now()))
	e := AuditEntry{ResourceID: "doc", Version: 1, ETag: "x"}
	if err := a.Record(e); err != nil {
		t.Fatalf("record: %v", err)
	}
	entries := a.Entries()
	if len(entries) != 1 || entries[0].ResourceID != "doc" {
		t.Fatalf("entries: %+v", entries)
	}
	// Returned slice must be a copy.
	entries[0].ResourceID = "mutated"
	if a.Entries()[0].ResourceID != "doc" {
		t.Fatal("Entries() must return a copy")
	}
}

func TestFaultInjectionFailNext(t *testing.T) {
	a := NewAudit(clock.NewFake(time.Now()))
	a.SetFaults(Faults{FailNext: 2})

	if err := a.Record(AuditEntry{}); !errors.Is(err, ErrInjected) {
		t.Fatalf("first call: %v", err)
	}
	if err := a.Record(AuditEntry{}); !errors.Is(err, ErrInjected) {
		t.Fatalf("second call: %v", err)
	}
	if err := a.Record(AuditEntry{}); err != nil {
		t.Fatalf("third call should succeed: %v", err)
	}
	if got := a.GetFaults().FailNext; got != 0 {
		t.Fatalf("FailNext should be consumed, got %d", got)
	}
	if len(a.Entries()) != 1 {
		t.Fatalf("only the successful call may be recorded, got %d", len(a.Entries()))
	}
}

func TestFaultInjectionLatency(t *testing.T) {
	a := NewAudit(clock.NewFake(time.Now()))
	a.SetFaults(Faults{LatencyMs: 20})
	start := time.Now()
	if err := a.Record(AuditEntry{}); err != nil {
		t.Fatalf("record: %v", err)
	}
	if elapsed := time.Since(start); elapsed < 20*time.Millisecond {
		t.Fatalf("latency not applied: %v", elapsed)
	}
}
