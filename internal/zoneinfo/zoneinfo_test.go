package zoneinfo

import (
	"strings"
	"testing"
	"time"
)

func TestLoadKnownZones(t *testing.T) {
	for _, name := range []string{"UTC", "America/New_York", "Asia/Tokyo", "Europe/London", "US/Eastern"} {
		loc, err := Load(name)
		if err != nil {
			t.Fatalf("Load(%q): %v", name, err)
		}
		if loc.String() != name {
			t.Fatalf("location name=%q want %q", loc.String(), name)
		}
	}
}

func TestLoadUnknownZone(t *testing.T) {
	if _, err := Load("Mars/Olympus"); err == nil {
		t.Fatal("expected error for unknown zone")
	} else if !strings.Contains(err.Error(), "not found") {
		t.Fatalf("error should say not found: %v", err)
	}
}

func TestNamesIncludesPrimaryZoneAndAlias(t *testing.T) {
	names, err := Names()
	if err != nil {
		t.Fatal(err)
	}
	if len(names) < 500 {
		t.Fatalf("implausibly few zones: %d", len(names))
	}
	has := func(want string) bool {
		for _, n := range names {
			if n == want {
				return true
			}
		}
		return false
	}
	if !has("America/New_York") || !has("US/Eastern") {
		t.Fatalf("primary zone or alias missing from %d names", len(names))
	}
}

func TestEmbeddedTransitionAgreesWithTZif(t *testing.T) {
	// Sanity-check transition decoding: the 2024 NY spring gap starts at
	// 07:00 UTC. A second before, offset is -5:00; at/after, -4:00.
	loc, err := Load("America/New_York")
	if err != nil {
		t.Fatal(err)
	}
	before := time.Unix(1710053999, 0) // 2024-03-10 06:59:59 UTC
	at := time.Unix(1710054000, 0)     // 2024-03-10 07:00:00 UTC
	if _, off := before.In(loc).Zone(); off != -5*3600 {
		t.Fatalf("pre-transition offset=%d", off)
	}
	if _, off := at.In(loc).Zone(); off != -4*3600 {
		t.Fatalf("post-transition offset=%d", off)
	}
}
