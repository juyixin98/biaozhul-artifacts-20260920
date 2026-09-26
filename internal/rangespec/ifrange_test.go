package rangespec

import (
	"net/http"
	"testing"
	"time"
)

func TestIfRange(t *testing.T) {
	etag := `"v1"`
	lastMod := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)

	tests := []struct {
		name      string
		header    string
		etag      string
		lastMod   time.Time
		wantHonor bool
	}{
		{"no header", "", etag, lastMod, true},
		{"matching etag", etag, etag, lastMod, true},
		{"mismatched etag", `"v2"`, etag, lastMod, false},
		{"weak etag in if-range", `W/"v1"`, etag, lastMod, false},
		{"future date honors", time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC).Format(http.TimeFormat), etag, lastMod, true},
		{"date equal to last-modified honors", lastMod.Format(http.TimeFormat), etag, lastMod, true},
		{"past date ignores", time.Date(2000, 1, 1, 0, 0, 0, 0, time.UTC).Format(http.TimeFormat), etag, lastMod, false},
		{"garbage date ignores", "not-a-date-or-etag", etag, lastMod, false},
		{"date with no last-modified metadata", time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC).Format(http.TimeFormat), etag, time.Time{}, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := IfRange(tc.header, tc.etag, tc.lastMod); got != tc.wantHonor {
				t.Errorf("IfRange(%q) = %v, want %v", tc.header, got, tc.wantHonor)
			}
		})
	}
}
