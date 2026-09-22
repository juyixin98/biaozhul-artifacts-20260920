package timeutil

import (
	"testing"
	"time"
)

func TestIsNightWrapping(t *testing.T) {
	sh := load("Asia/Shanghai")    // UTC+8, no DST
	ny := load("America/New_York") // UTC-4 in September (DST)

	cases := []struct {
		name     string
		when     time.Time
		loc      *time.Location
		isNight  bool
		ownLabel string // expected owning local date YYYY-MM-DD
	}{
		{
			name:     "shanghai 20:00 local starts night",
			when:     time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC),
			loc:      sh,
			isNight:  true,
			ownLabel: "2026-09-20",
		},
		{
			name:     "shanghai 05:59 local belongs previous night",
			when:     time.Date(2026, 9, 20, 21, 59, 0, 0, time.UTC),
			loc:      sh,
			isNight:  true,
			ownLabel: "2026-09-20",
		},
		{
			name:     "shanghai 06:00 local is outside",
			when:     time.Date(2026, 9, 20, 22, 0, 0, 0, time.UTC),
			loc:      sh,
			isNight:  false,
			ownLabel: "2026-09-21",
		},
		{
			name:     "shanghai 19:59 local is outside",
			when:     time.Date(2026, 9, 20, 11, 59, 0, 0, time.UTC),
			loc:      sh,
			isNight:  false,
			ownLabel: "2026-09-20",
		},
		{
			name:     "ny 22:00 local (02:00Z) night",
			when:     time.Date(2026, 9, 21, 2, 0, 0, 0, time.UTC),
			loc:      ny,
			isNight:  true,
			ownLabel: "2026-09-20",
		},
		{
			name:     "ny 05:59 local (09:59Z) previous night tail",
			when:     time.Date(2026, 9, 21, 9, 59, 0, 0, time.UTC),
			loc:      ny,
			isNight:  true,
			ownLabel: "2026-09-20",
		},
		{
			name:     "ny 06:00 local (10:00Z) outside",
			when:     time.Date(2026, 9, 21, 10, 0, 0, 0, time.UTC),
			loc:      ny,
			isNight:  false,
			ownLabel: "2026-09-21",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, day := IsNight(tc.when, tc.loc, 20, 6)
			if got != tc.isNight {
				t.Fatalf("IsNight = %v, want %v", got, tc.isNight)
			}
			if got && day.Format("2006-01-02") != tc.ownLabel {
				t.Fatalf("owning date = %s, want %s", day.Format("2006-01-02"), tc.ownLabel)
			}
		})
	}
}

func TestNightBoundsWrapping(t *testing.T) {
	sh := load("Asia/Shanghai")
	day := time.Date(2026, 9, 20, 0, 0, 0, 0, sh)
	start, end := NightBounds(day, 20, 6)
	wantStart := time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC) // 20:00 +0800
	wantEnd := time.Date(2026, 9, 20, 22, 0, 0, 0, time.UTC)   // 06:00 next day +0800
	if !start.Equal(wantStart) || !end.Equal(wantEnd) {
		t.Fatalf("bounds = %s..%s, want %s..%s", start, end, wantStart, wantEnd)
	}
}

func TestNightBoundsDST(t *testing.T) {
	// New York night spanning the Nov 1 2026 DST transition (EDT UTC-4 -> EST
	// UTC-5). The night of Oct 31 starts at 00:00Z; its 06:00 local end is
	// 11:00Z (an 11-hour window instead of the usual 10).
	ny := load("America/New_York")
	day := time.Date(2026, 10, 31, 0, 0, 0, 0, ny)
	start, end := NightBounds(day, 20, 6)
	wantStart := time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC) // 20:00 EDT
	wantEnd := time.Date(2026, 11, 1, 11, 0, 0, 0, time.UTC)  // 06:00 EST
	if !start.Equal(wantStart) {
		t.Fatalf("start = %s, want %s", start, wantStart)
	}
	if !end.Equal(wantEnd) {
		t.Fatalf("end = %s, want %s", end, wantEnd)
	}
}

func TestBurstWindowStart(t *testing.T) {
	w := 10 * time.Minute
	in := time.Date(2026, 9, 20, 12, 9, 59, 0, time.UTC)
	start := BurstWindowStart(in, w)
	want := time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC)
	if !start.Equal(want) {
		t.Fatalf("start = %s, want %s", start, want)
	}
	// Exactly on the boundary belongs to the new bucket.
	if got := BurstWindowStart(time.Date(2026, 9, 20, 12, 10, 0, 0, time.UTC), w); !got.Equal(time.Date(2026, 9, 20, 12, 10, 0, 0, time.UTC)) {
		t.Fatalf("boundary alignment wrong: %s", got)
	}
}

func TestDateLabelStableAcrossOffsets(t *testing.T) {
	sh := load("Asia/Shanghai")
	// 2026-09-20 00:30 local = 2026-09-19 16:30 UTC: local date must be the 20th.
	in := time.Date(2026, 9, 19, 16, 30, 0, 0, time.UTC)
	if got := DateLabel(in, sh).Format("2006-01-02"); got != "2026-09-20" {
		t.Fatalf("label = %s, want 2026-09-20", got)
	}
}

func load(name string) *time.Location {
	loc, err := time.LoadLocation(name)
	if err != nil {
		panic(err)
	}
	return loc
}
