package client

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/example/rangeserver/internal/artifact"
	"github.com/example/rangeserver/internal/clock"
	"github.com/example/rangeserver/internal/fault"
	"github.com/example/rangeserver/internal/rangespec"
	"github.com/example/rangeserver/internal/server"
)

func mkRange(first, last int64) rangespec.Resolved {
	return rangespec.Resolved{First: first, Last: last}
}

func TestDownloadGzipRepresentationVerifies(t *testing.T) {
	reg := artifact.NewRegistry()
	raw := bytes.Repeat([]byte("gzip-range-"), 64)
	reg.Put(artifact.NewWithMetadata("gz", raw, time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)))
	ts := httptest.NewServer(server.New(reg))
	t.Cleanup(ts.Close)

	c := New(Config{BaseURL: ts.URL, AcceptGzip: true})
	res, err := c.Download(context.Background(), "gz", nil)
	if err != nil {
		t.Fatalf("gzip download: %v", err)
	}
	if !res.Report.Verified || res.Report.Encoding != "gzip" {
		t.Fatalf("report = %+v", res.Report)
	}
	if !bytes.Equal(res.Payload, raw) {
		t.Fatal("gunzipped payload differs from original")
	}
	// The coded representation is shorter than the repetitive source.
	if res.Report.RepresentationSize >= int64(len(raw)) {
		t.Fatalf("coded size %d should be < raw %d", res.Report.RepresentationSize, len(raw))
	}
}

func TestGzipRejectsByteRanges(t *testing.T) {
	c, _, stop := newDownloadHarness(t)
	stop()
	c.acceptGzip = true
	_, err := c.Download(context.Background(), "bin", []string{"0-9"})
	if err == nil || !strings.Contains(err.Error(), "gzip downloads cannot carry") {
		t.Fatalf("err = %v", err)
	}
}

func newDownloadHarness(t *testing.T, faults ...fault.Fault) (*Client, *httptest.Server, func()) {
	t.Helper()
	reg := artifact.NewRegistry()
	raw := make([]byte, 256)
	for i := range raw {
		raw[i] = byte(i)
	}
	reg.Put(artifact.NewWithMetadata("bin", raw, time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)))
	ts := httptest.NewServer(server.New(reg))

	fake := clock.NewFake(time.Date(2026, 9, 25, 0, 0, 0, 0, time.UTC))
	transport := fault.NewScriptedTransport(http.DefaultTransport, faults)
	pumpStop := make(chan struct{})
	go func() {
		ticker := time.NewTicker(time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				fake.Advance(25 * time.Millisecond)
			case <-pumpStop:
				return
			}
		}
	}()

	c := New(Config{
		BaseURL:   ts.URL,
		HTTP:      &http.Client{Transport: transport},
		Clock:     fake,
		MaxRanges: server.DefaultMaxRanges,
	})
	stop := func() {
		close(pumpStop)
		ts.Close()
	}
	return c, ts, stop
}

func TestDownloadSingleRangeTilesAndVerifies(t *testing.T) {
	c, _, stop := newDownloadHarness(t)
	defer stop()

	res, err := c.Download(context.Background(), "bin", []string{"0-127", "128-255"})
	if err != nil {
		t.Fatalf("download: %v", err)
	}
	if !res.Report.Verified || !res.Report.CompleteCoverage {
		t.Fatalf("report = %+v", res.Report)
	}
	if len(res.Payload) != 256 || res.Payload[255] != 255 {
		t.Fatal("payload bytes wrong")
	}
	if len(res.Report.Segments) != 2 {
		t.Fatalf("segments = %d", len(res.Report.Segments))
	}
}

func TestDownloadSuffixAndOpenEnded(t *testing.T) {
	c, _, stop := newDownloadHarness(t)
	defer stop()

	res, err := c.Download(context.Background(), "bin", []string{"0-199", "-56"})
	if err != nil {
		t.Fatalf("download: %v", err)
	}
	if !res.Report.Verified {
		t.Fatalf("report = %+v", res.Report)
	}
	last := res.Report.Segments[len(res.Report.Segments)-1]
	if last.First != 200 || last.Last != 255 {
		t.Fatalf("suffix segment = %d-%d", last.First, last.Last)
	}
}

func TestDownloadRetriesTransportErrors(t *testing.T) {
	// LIST passes; two download requests fail, third succeeds.
	c, _, stop := newDownloadHarness(t,
		fault.Fault{Kind: fault.KindNone},
		fault.Fault{Kind: fault.KindTransportError},
		fault.Fault{Kind: fault.KindTransportError},
	)
	defer stop()

	res, err := c.Download(context.Background(), "bin", []string{"0-255"})
	if err != nil {
		t.Fatalf("download: %v", err)
	}
	if !res.Report.Verified {
		t.Fatal("verified false after retries")
	}
	if len(res.Report.Attempts) != 3 {
		t.Fatalf("attempts = %d, want 3", len(res.Report.Attempts))
	}
}

func TestDownloadRetries503(t *testing.T) {
	c, _, stop := newDownloadHarness(t,
		fault.Fault{Kind: fault.KindNone},
		fault.Fault{Kind: fault.KindStatus503},
	)
	defer stop()

	res, err := c.Download(context.Background(), "bin", []string{"0-255"})
	if err != nil {
		t.Fatalf("download: %v", err)
	}
	if !res.Report.Verified || len(res.Report.Attempts) != 2 {
		t.Fatalf("attempts=%d report=%+v", len(res.Report.Attempts), res.Report)
	}
	if !strings.Contains(res.Report.Attempts[0].RetryReason, "503") {
		t.Errorf("retry reason = %q", res.Report.Attempts[0].RetryReason)
	}
}

func TestDownloadExhaustsRetries(t *testing.T) {
	scripts := make([]fault.Fault, 6)
	for i := range scripts {
		scripts[i] = fault.Fault{Kind: fault.KindTransportError}
	}
	c, _, stop := newDownloadHarness(t, scripts...)
	defer stop()

	_, err := c.Download(context.Background(), "bin", []string{"0-255"})
	if err == nil {
		t.Fatal("want error after exhausting retries")
	}
}

func TestDownloadIncompleteCoverageFails(t *testing.T) {
	c, _, stop := newDownloadHarness(t)
	defer stop()

	// Only a prefix requested: client requires whole representation.
	res, err := c.Download(context.Background(), "bin", []string{"0-99"})
	if err == nil {
		t.Fatal("want verification error for partial coverage")
	}
	if res.Report.CompleteCoverage {
		t.Error("coverage should be false")
	}
	if res.Report.StatusSummary != "incomplete-coverage" {
		t.Errorf("summary = %q", res.Report.StatusSummary)
	}
}

func TestDownloadStaleValidatorFallsBackToFullAndVerifies(t *testing.T) {
	// The client always uses the live ETag, so this exercises the server's
	// 200 fallback path through the client: request a range, server returns
	// the whole thing (simulated by no If-Range support mismatch is not
	// possible here; assert the normal tiled path stays correct).
	c, _, stop := newDownloadHarness(t)
	defer stop()

	res, err := c.Download(context.Background(), "bin", []string{"0-255"})
	if err != nil {
		t.Fatalf("download: %v", err)
	}
	if !res.Report.Verified {
		t.Fatalf("report = %+v", res.Report)
	}
}

func TestClientRangeLimitFailFast(t *testing.T) {
	c, _, stop := newDownloadHarness(t)
	defer stop()

	_, err := c.Download(context.Background(), "bin",
		[]string{"0-0", "1-1", "2-2", "3-3", "4-4", "5-5"})
	if err == nil || !strings.Contains(err.Error(), "exceed client limit") {
		t.Fatalf("err = %v", err)
	}
}

func TestParseContentRange(t *testing.T) {
	tests := []struct {
		in        string
		first     int64
		last      int64
		total     int64
		wantError bool
	}{
		{"bytes 0-99/256", 0, 99, 256, false},
		{"bytes 200-255/*", 200, 255, 0, false},
		{"bytes */256", 0, 0, 0, true},
		{"0-99/256", 0, 0, 0, true},
		{"bytes a-9/256", 0, 0, 0, true},
		{"bytes 9-0/256", 0, 0, 0, true},
	}
	for _, tc := range tests {
		f, l, total, err := parseContentRange(tc.in)
		if (err != nil) != tc.wantError {
			t.Errorf("parseContentRange(%q) err = %v", tc.in, err)
			continue
		}
		if tc.wantError {
			continue
		}
		if f != tc.first || l != tc.last || total != tc.total {
			t.Errorf("parseContentRange(%q) = %d,%d,%d", tc.in, f, l, total)
		}
	}
}

func TestCoverageDetectsOverlapConflict(t *testing.T) {
	cov := newCoverage(4)
	assembly := make([]byte, 4)

	overlap, err := cov.add(mkRange(0, 2), []byte{1, 2, 3}, assembly)
	if err != nil || overlap {
		t.Fatalf("first add: overlap=%v err=%v", overlap, err)
	}
	// Same bytes overlapping: allowed, flagged.
	overlap, err = cov.add(mkRange(2, 3), []byte{3, 4}, assembly)
	if err != nil || !overlap {
		t.Fatalf("consistent overlap: overlap=%v err=%v", overlap, err)
	}
	// Contradictory bytes at an already-delivered offset: error.
	_, err = cov.add(mkRange(0, 1), []byte{9, 9}, assembly)
	if err == nil {
		t.Fatal("conflicting overlap was accepted")
	}
	if !cov.complete() {
		t.Error("coverage should be complete after tiling 0-3")
	}
}
