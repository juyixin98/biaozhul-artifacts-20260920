package server

import (
	"net/http"
	"testing"
	"time"
)

func sampleRep() (representation, time.Time) {
	lm := time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)
	return representation{
		data:        []byte("hello"),
		contentType: "text/plain",
		etag:        `"abc123"`,
	}, lm
}

func TestIfRangeNoHeader(t *testing.T) {
	rep, lm := sampleRep()
	if !ifRangeAllowsRange("", rep, lm) {
		t.Error("absent If-Range must allow ranges")
	}
}

func TestIfRangeStrongETag(t *testing.T) {
	rep, lm := sampleRep()
	if !ifRangeAllowsRange(`"abc123"`, rep, lm) {
		t.Error("matching strong ETag must allow ranges")
	}
	if ifRangeAllowsRange(`"other"`, rep, lm) {
		t.Error("mismatched strong ETag must force full rep")
	}
}

func TestIfRangeWeakETagIgnored(t *testing.T) {
	rep, lm := sampleRep()
	if !ifRangeAllowsRange(`W/"anything"`, rep, lm) {
		t.Error("a weak entity-tag in If-Range must make the field ignorable")
	}
}

func TestIfRangeDate(t *testing.T) {
	rep, lm := sampleRep()
	later := lm.Add(time.Hour).UTC().Format(http.TimeFormat)
	earlier := lm.Add(-time.Hour).UTC().Format(http.TimeFormat)
	exact := lm.UTC().Format(http.TimeFormat)

	if !ifRangeAllowsRange(later, rep, lm) {
		t.Error("date not earlier than Last-Modified must allow ranges")
	}
	if ifRangeAllowsRange(earlier, rep, lm) {
		t.Error("stale date must force full rep")
	}
	if !ifRangeAllowsRange(exact, rep, lm) {
		t.Error("exact-match date must allow ranges")
	}
}

func TestIfRangeGarbageIgnored(t *testing.T) {
	rep, lm := sampleRep()
	if !ifRangeAllowsRange("not-a-date-or-tag", rep, lm) {
		t.Error("unparseable If-Range must be ignored")
	}
}

func TestGzipWeight(t *testing.T) {
	cases := []struct {
		h    string
		want float64
	}{
		{"", 0},
		{"gzip", 1},
		{"gzip;q=0.5", 0.5},
		{"gzip;q=0", 0},
		{"deflate", 0},
		{"*;q=0.7", 0.7},
		{"gzip;q=1, deflate", 1},
	}
	for _, tc := range cases {
		if got := gzipWeight(tc.h); got != tc.want {
			t.Errorf("gzipWeight(%q) = %v, want %v", tc.h, got, tc.want)
		}
	}
}
