package main

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/example/rangeserver/internal/artifact"
	"github.com/example/rangeserver/internal/client"
	"github.com/example/rangeserver/internal/fault"
)

// Each scenario returns a structured ScenarioResult. Client-driven
// scenarios additionally embed the full client Report (attempts,
// segments, verification outcome).

func (h *harness) prefixRange() ScenarioResult {
	// "bytes=0-99" -> 206, single range, first 100 bytes.
	res, err := h.goodClient().Download(context.Background(), "binary256", []string{"0-99", "100-255"})
	r := ScenarioResult{
		Name:        "prefix-range",
		Expectation: "bytes=0-99 (tiled with 100-255) yields 206 multipart and verifies",
		Report:      reportOrNil(res),
	}
	if err != nil {
		r.Error = err.Error()
		return r
	}
	rep := res.Report
	r.Passed = rep.Verified && rep.CompleteCoverage &&
		rep.Attempts[len(rep.Attempts)-1].Status == http.StatusPartialContent &&
		rep.Segments[0].First == 0 && rep.Segments[0].Last == 99
	return r
}

func (h *harness) suffixRange() ScenarioResult {
	// "bytes=-64" -> final 64 bytes, tiled with 0-191 for full coverage.
	res, err := h.goodClient().Download(context.Background(), "binary256", []string{"0-191", "-64"})
	r := ScenarioResult{
		Name:        "suffix-range",
		Expectation: "bytes=-64 yields the final 64 bytes (192-255); suffix tiles to the original",
		Report:      reportOrNil(res),
	}
	if err != nil {
		r.Error = err.Error()
		return r
	}
	rep := res.Report
	last := rep.Segments[len(rep.Segments)-1]
	r.Passed = rep.Verified && rep.CompleteCoverage && last.First == 192 && last.Last == 255
	return r
}

func (h *harness) openEndedRange() ScenarioResult {
	// "bytes=200-" -> bytes 200..255; assert server semantics directly.
	return h.rawExpect("open-ended-range",
		"bytes=200- yields 206 bytes 200-255/256 with 56 bytes",
		"binary256",
		map[string]string{"Range": "bytes=200-"},
		func(resp *http.Response, body []byte) (bool, string) {
			if resp.StatusCode != http.StatusPartialContent {
				return false, fmt.Sprintf("status=%d", resp.StatusCode)
			}
			if got := resp.Header.Get("Content-Range"); got != "bytes 200-255/256" {
				return false, "content-range=" + got
			}
			if len(body) != 56 {
				return false, fmt.Sprintf("body length=%d", len(body))
			}
			return true, ""
		})
}

func (h *harness) multiRangeTiled() ScenarioResult {
	// Three adjacent non-overlapping ranges tile the 256-byte artifact.
	res, err := h.goodClient().Download(context.Background(), "binary256",
		[]string{"0-99", "100-199", "200-255"})
	r := ScenarioResult{
		Name:        "multi-range-tiled",
		Expectation: "three adjacent ranges return multipart/byteranges and reassemble to the original",
		Report:      reportOrNil(res),
	}
	if err != nil {
		r.Error = err.Error()
		return r
	}
	rep := res.Report
	r.Passed = rep.Verified && rep.CompleteCoverage &&
		rep.Attempts[len(rep.Attempts)-1].Decision == "multipart" &&
		len(rep.Segments) == 3
	return r
}

func (h *harness) overlappingRanges() ScenarioResult {
	// Overlapping ranges "0-119" and "100-255" coalesce server-side and
	// still reassemble to the exact original bytes.
	res, err := h.goodClient().Download(context.Background(), "binary256",
		[]string{"0-119", "100-255"})
	r := ScenarioResult{
		Name:        "overlapping-ranges",
		Expectation: "overlapping ranges coalesce server-side; no duplicated/contradictory bytes; hash matches",
		Report:      reportOrNil(res),
	}
	if err != nil {
		r.Error = err.Error()
		return r
	}
	rep := res.Report
	r.Passed = rep.Verified && rep.CompleteCoverage && rep.OverlappingRangesRequested &&
		len(rep.Segments) == 1 &&
		rep.Segments[0].First == 0 && rep.Segments[0].Last == 255
	return r
}

func (h *harness) unsatisfiableRange() ScenarioResult {
	return h.rawExpect("unsatisfiable-range",
		"bytes=1000- on a 256-byte artifact yields 416 with Content-Range bytes */256",
		"binary256",
		map[string]string{"Range": "bytes=1000-"},
		func(resp *http.Response, body []byte) (bool, string) {
			if resp.StatusCode != http.StatusRequestedRangeNotSatisfiable {
				return false, fmt.Sprintf("status=%d", resp.StatusCode)
			}
			if got := resp.Header.Get("Content-Range"); got != "bytes */256" {
				return false, "content-range=" + got
			}
			if resp.Header.Get("X-Range-Decision") != "unsatisfiable-416" {
				return false, "decision=" + resp.Header.Get("X-Range-Decision")
			}
			return true, ""
		})
}

func (h *harness) zeroLengthArtifact() ScenarioResult {
	r := h.rawExpect("zero-length-artifact",
		"any range on a zero-length artifact yields 416 with bytes */0 and a status body",
		"empty",
		map[string]string{"Range": "bytes=0-0"},
		func(resp *http.Response, body []byte) (bool, string) {
			if resp.StatusCode != http.StatusRequestedRangeNotSatisfiable {
				return false, fmt.Sprintf("range status=%d", resp.StatusCode)
			}
			if got := resp.Header.Get("Content-Range"); got != "bytes */0" {
				return false, "content-range=" + got
			}
			if len(body) == 0 {
				return false, "416 must carry a status body"
			}
			return true, ""
		})
	if !r.Passed {
		return r
	}
	// A plain GET must still succeed with 200 and an empty body.
	also, why := h.rawCheckOnly("empty", nil, func(resp *http.Response, body []byte) (bool, string) {
		if resp.StatusCode != http.StatusOK {
			return false, fmt.Sprintf("get status=%d", resp.StatusCode)
		}
		if len(body) != 0 {
			return false, fmt.Sprintf("get body length=%d", len(body))
		}
		if resp.Header.Get("Accept-Ranges") != "bytes" {
			return false, "missing Accept-Ranges"
		}
		return true, ""
	})
	if !also {
		r.Passed = false
		r.Detail["plain_get"] = why
	} else {
		r.Detail["plain_get"] = "200 empty body, Accept-Ranges: bytes"
	}
	return r
}

func (h *harness) etagMismatch() ScenarioResult {
	// If-Range with a stale ETag: server ignores Range and sends 200 full.
	return h.rawExpect("etag-mismatch-if-range",
		"stale If-Range ETag causes the server to ignore Range and return 200 (full body)",
		"binary256",
		map[string]string{
			"Range":    "bytes=0-9",
			"If-Range": `"stale-etag"`,
		},
		func(resp *http.Response, body []byte) (bool, string) {
			if resp.StatusCode != http.StatusOK {
				return false, fmt.Sprintf("status=%d", resp.StatusCode)
			}
			if d := resp.Header.Get("X-Range-Decision"); d != "ignored-if-range" {
				return false, "decision=" + d
			}
			if len(body) != 256 {
				return false, fmt.Sprintf("full body length=%d", len(body))
			}
			if cr := resp.Header.Get("Content-Range"); cr != "" {
				return false, "unexpected Content-Range on full response: " + cr
			}
			return true, ""
		})
}

func (h *harness) ifRangeDateForms() ScenarioResult {
	var detail []string
	pass := true

	// Future date (after Last-Modified): representation unmodified -> 206.
	future := time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC).Format(http.TimeFormat)
	p1, d1 := h.rawCheckOnly("binary256", map[string]string{
		"Range": "bytes=0-9", "If-Range": future,
	}, func(resp *http.Response, body []byte) (bool, string) {
		if resp.StatusCode != http.StatusPartialContent {
			return false, fmt.Sprintf("status=%d", resp.StatusCode)
		}
		if resp.Header.Get("Content-Range") != "bytes 0-9/256" {
			return false, "content-range=" + resp.Header.Get("Content-Range")
		}
		return true, ""
	})
	detail = append(detail, "future-date: "+d1)
	pass = pass && p1

	// Past date (before Last-Modified): modified -> 200 full.
	past := time.Date(2000, 1, 1, 0, 0, 0, 0, time.UTC).Format(http.TimeFormat)
	p2, d2 := h.rawCheckOnly("binary256", map[string]string{
		"Range": "bytes=0-9", "If-Range": past,
	}, func(resp *http.Response, body []byte) (bool, string) {
		if resp.StatusCode != http.StatusOK {
			return false, fmt.Sprintf("status=%d", resp.StatusCode)
		}
		if d := resp.Header.Get("X-Range-Decision"); d != "ignored-if-range" {
			return false, "decision=" + d
		}
		if len(body) != 256 {
			return false, fmt.Sprintf("body length=%d", len(body))
		}
		return true, ""
	})
	detail = append(detail, "past-date: "+d2)
	pass = pass && p2

	return ScenarioResult{
		Name:        "if-range-date-forms",
		Expectation: "If-Range future-date honors the range (206); past-date ignores it (200)",
		Passed:      pass,
		Detail:      map[string]any{"checks": detail},
	}
}

func (h *harness) malformedRange() ScenarioResult {
	return h.rawExpect("malformed-range-ignored",
		"a syntactically invalid Range header is ignored and the full 200 representation is sent",
		"binary256",
		map[string]string{"Range": "bytes=9-1"},
		func(resp *http.Response, body []byte) (bool, string) {
			if resp.StatusCode != http.StatusOK {
				return false, fmt.Sprintf("status=%d", resp.StatusCode)
			}
			if d := resp.Header.Get("X-Range-Decision"); d != "ignored-malformed" {
				return false, "decision=" + d
			}
			if len(body) != 256 {
				return false, fmt.Sprintf("body length=%d", len(body))
			}
			return true, ""
		})
}

func (h *harness) tooManyRanges() ScenarioResult {
	// DefaultMaxRanges is 5; ask for 6 -> 400 with a clear error.
	return h.rawExpect("too-many-ranges",
		"more than 5 ranges are rejected with 400 rather than assembled",
		"binary256",
		map[string]string{"Range": "bytes=0-0,1-1,2-2,3-3,4-4,5-5"},
		func(resp *http.Response, body []byte) (bool, string) {
			if resp.StatusCode != http.StatusBadRequest {
				return false, fmt.Sprintf("status=%d", resp.StatusCode)
			}
			if d := resp.Header.Get("X-Range-Decision"); d != "rejected-range-limit" {
				return false, "decision=" + d
			}
			if !strings.Contains(string(body), "too many ranges") {
				return false, "body=" + string(body)
			}
			return true, ""
		})
}

func (h *harness) retryAfterTransportFaults() ScenarioResult {
	// Two synthetic transport failures on the download request, then
	// success. The FakeClock advances backoff instantly.
	c, stop := h.faultClient([]fault.Fault{
		{Kind: fault.KindNone}, // metadata LIST passes
		{Kind: fault.KindTransportError},
		{Kind: fault.KindTransportError},
	})
	defer stop()
	res, err := c.Download(context.Background(), "binary256",
		[]string{"0-127", "128-255"})
	r := ScenarioResult{
		Name:        "retry-after-transport-faults",
		Expectation: "client retries 2 injected transport errors and still verifies the original bytes",
		Report:      reportOrNil(res),
	}
	if err != nil {
		r.Error = err.Error()
		return r
	}
	rep := res.Report
	r.Passed = rep.Verified && len(rep.Attempts) == 3 &&
		rep.Attempts[0].Error != "" && rep.Attempts[1].Error != "" &&
		rep.Attempts[2].Status == http.StatusPartialContent
	return r
}

func (h *harness) retryAfterTruncatedBody() ScenarioResult {
	// First download attempt gets an unexpectedly-closed body; retry
	// succeeds.
	c, stop := h.faultClient([]fault.Fault{
		{Kind: fault.KindNone}, // LIST
		{Kind: fault.KindTruncated},
	})
	defer stop()
	res, err := c.Download(context.Background(), "binary256", []string{"0-255"})
	r := ScenarioResult{
		Name:        "retry-after-truncated-body",
		Expectation: "a body that errors mid-read is retried; final bytes verify",
		Report:      reportOrNil(res),
	}
	if err != nil {
		r.Error = err.Error()
		return r
	}
	rep := res.Report
	r.Passed = rep.Verified && len(rep.Attempts) == 2 &&
		strings.Contains(rep.Attempts[0].Error, "unexpected EOF")
	return r
}

func (h *harness) gzipRepresentation() ScenarioResult {
	// Full gzip GET: coded representation with distinct ETag; decodes back
	// to the 300 canonical bytes.
	r := h.rawExpect("gzip-selected-representation",
		"Accept-Encoding: gzip returns a gzip-coded 200 with Vary and its own length/ETag",
		"lorem300",
		map[string]string{"Accept-Encoding": "gzip"},
		func(resp *http.Response, body []byte) (bool, string) {
			if resp.StatusCode != http.StatusOK {
				return false, fmt.Sprintf("status=%d", resp.StatusCode)
			}
			if ce := resp.Header.Get("Content-Encoding"); ce != "gzip" {
				return false, "content-encoding=" + ce
			}
			if resp.Header.Get("Vary") == "" {
				return false, "missing Vary"
			}
			if int64(len(body)) != resp.ContentLength {
				return false, "body length disagrees with Content-Length"
			}
			if len(body) == 300 {
				return false, "gzip body unexpectedly identical length to resource"
			}
			decoded, err := artifact.Gunzip(body)
			if err != nil {
				return false, "gunzip: " + err.Error()
			}
			if len(decoded) != 300 || decoded[0] != 'A' {
				return false, fmt.Sprintf("decoded length=%d", len(decoded))
			}
			return true, ""
		})
	if !r.Passed {
		return r
	}
	// Ranges are measured against the coded representation. First learn the
	// coded length, then request the coded prefix and confirm Content-Range
	// reports the coded total and returns gzip bytes decodable on their own
	// only as a whole... a gzip prefix alone is not independently
	// gunzippable, so here we assert offset bookkeeping, not decoding.
	pass, why := h.rawCheckOnly("lorem300",
		map[string]string{"Accept-Encoding": "gzip", "Range": "bytes=0-9"},
		func(resp *http.Response, body []byte) (bool, string) {
			if resp.StatusCode != http.StatusPartialContent {
				return false, fmt.Sprintf("status=%d", resp.StatusCode)
			}
			cr := resp.Header.Get("Content-Range")
			if !strings.HasPrefix(cr, "bytes 0-9/") || strings.HasSuffix(cr, "/300") {
				return false, "content-range should cite coded length, got " + cr
			}
			if ce := resp.Header.Get("Content-Encoding"); ce != "gzip" {
				return false, "content-encoding=" + ce
			}
			if len(body) != 10 {
				return false, fmt.Sprintf("body length=%d", len(body))
			}
			return true, "content-range=" + cr
		})
	if !pass {
		r.Passed = false
		r.Detail["coded_range"] = why
	} else {
		r.Detail["coded_range"] = why
	}
	return r
}

// ---- raw HTTP helpers ----

// rawExpect performs exactly one raw request and applies the predicate.
func (h *harness) rawExpect(name, expectation, id string, headers map[string]string,
	check func(*http.Response, []byte) (bool, string)) ScenarioResult {
	r := ScenarioResult{Name: name, Expectation: expectation}
	pass, detail := h.rawCheckOnly(id, headers, check)
	r.Passed = pass
	r.Detail = map[string]any{"raw_check": detail}
	return r
}

// rawCheckOnly issues one GET with the given headers and runs check.
func (h *harness) rawCheckOnly(id string, headers map[string]string,
	check func(*http.Response, []byte) (bool, string)) (bool, string) {
	req, _ := http.NewRequest(http.MethodGet, h.baseURL+"/artifacts/"+id, nil)
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := h.raw.Do(req)
	if err != nil {
		return false, "request error: " + err.Error()
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return false, "reading body: " + err.Error()
	}
	ok, why := check(resp, body)
	if !ok {
		return false, fmt.Sprintf("status=%d decision=%s: %s",
			resp.StatusCode, resp.Header.Get("X-Range-Decision"), why)
	}
	return true, fmt.Sprintf("status=%d decision=%s",
		resp.StatusCode, resp.Header.Get("X-Range-Decision"))
}

func reportOrNil(res *client.DownloadResult) *client.Report {
	if res == nil {
		return nil
	}
	return &res.Report
}
