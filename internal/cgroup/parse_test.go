package cgroup

import (
	"strings"
	"testing"
)

func TestParseCPUStat(t *testing.T) {
	s, warnings, err := ParseCPUStat(`usage_usec 123456
user_usec 100000
system_usec 23456
nr_periods 7
nr_throttled 2
throttled_usec 4000
`)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(warnings) != 0 {
		t.Fatalf("unexpected warnings: %v", warnings)
	}
	if s.UsageUsec != 123456 || s.UserUsec != 100000 || s.SystemUsec != 23456 ||
		s.NrPeriods != 7 || s.NrThrottled != 2 || s.ThrottledUsec != 4000 {
		t.Fatalf("wrong parse: %+v", s)
	}
}

func TestParseCPUStatUnknownKeyWarns(t *testing.T) {
	_, warnings, err := ParseCPUStat("usage_usec 1\nfuture_key 9\n")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(warnings) != 1 || !strings.Contains(warnings[0], "future_key") {
		t.Fatalf("expected unknown-key warning, got %v", warnings)
	}
}

func TestParseCPUStatMalformed(t *testing.T) {
	if _, _, err := ParseCPUStat("usage_usec notanumber\n"); err == nil {
		t.Fatal("expected error for non-numeric value")
	}
	if _, _, err := ParseCPUStat("usage_usec 1 2 3\n"); err == nil {
		t.Fatal("expected error for wrong field count")
	}
}

func TestParseMemoryEvents(t *testing.T) {
	e, _, err := ParseMemoryEvents("low 0\nhigh 1\nmax 2\noom 3\noom_kill 4\noom_group_kill 0\n")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if e.High != 1 || e.Max != 2 || e.Oom != 3 || e.OomKill != 4 {
		t.Fatalf("wrong parse: %+v", e)
	}
}

func TestParseMemoryMax(t *testing.T) {
	v, unlimited, err := ParseMemoryMax("268435456\n")
	if err != nil || unlimited || v != 268435456 {
		t.Fatalf("numeric: v=%d unlimited=%v err=%v", v, unlimited, err)
	}
	if _, unlimited, err = ParseMemoryMax("max\n"); err != nil || !unlimited {
		t.Fatalf("max: unlimited=%v err=%v", unlimited, err)
	}
	if _, _, err = ParseMemoryMax("-5\n"); err == nil {
		t.Fatal("expected error for negative value")
	}
}

func TestParsePressure(t *testing.T) {
	p, _, err := ParsePressure("some avg10=1.50 avg60=0.25 avg300=0.00 total=12345\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n", "cpu.pressure")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if p.Some.Avg10 != 1.5 || p.Some.Avg60 != 0.25 || p.Some.Total != 12345 {
		t.Fatalf("wrong parse: %+v", p)
	}
	if _, _, err := ParsePressure("bogus avg10=1.0 avg60=0.0 avg300=0.0 total=0\n", "cpu.pressure"); err == nil {
		t.Fatal("expected error for unknown line type")
	}
}
