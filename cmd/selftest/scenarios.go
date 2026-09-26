package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"httprange/internal/client"
	"httprange/internal/fault"
	"httprange/internal/report"
)

// scenario is one acceptance case. run returns the observed response record
// and a non-nil error when an expectation is unmet.
type scenario struct {
	id, name, given, when, then string
	run                         func(e *env) (report.ResponseSpec, error)
}

func scenarios() []scenario {
	return []scenario{
		{
			id: "R01", name: "full representation baseline",
			given: "sample.bin, 1024 immutable bytes",
			when:  "GET /artifacts/sample.bin (no Range)",
			then:  "200, Accept-Ranges: bytes, strong ETag, declared Content-Length",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				rep, err := c.FetchRepresentation(idSample, "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if len(rep.Body) != sampleSize {
					return report.ResponseSpec{}, failf("body length %d != %d", len(rep.Body), sampleSize)
				}
				if rep.Headers.Get("Accept-Ranges") != "bytes" {
					return report.ResponseSpec{}, failf("missing Accept-Ranges: bytes")
				}
				if !strings.HasPrefix(rep.ETag, `"`) {
					return report.ResponseSpec{}, failf("ETag not strong/quoted: %q", rep.ETag)
				}
				if rep.Headers.Get("Content-Length") != fmt.Sprintf("%d", sampleSize) {
					return report.ResponseSpec{}, failf("Content-Length mismatch")
				}
				return respSpec(http.StatusOK, rep.Headers, int64(len(rep.Body)), rep.Body), nil
			},
		},
		{
			id: "R02", name: "prefix range",
			given: "sample.bin (1024 bytes)",
			when:  `GET with Range: bytes=0-99`,
			then:  "206, Content-Range: bytes 0-99/1024, first 100 bytes",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				rep, _ := c.FetchRepresentation(idSample, "")
				out, err := c.FetchRanges(idSample, "0-99", "", "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.StatusCode != 206 || out.Single == nil {
					return report.ResponseSpec{}, failf("want single 206, got %d", out.StatusCode)
				}
				if out.ContentRange != "bytes 0-99/1024" {
					return report.ResponseSpec{}, failf("Content-Range %q", out.ContentRange)
				}
				if err := client.Verify(out.Single.Payload, rep.Body[0:100]); err != nil {
					return report.ResponseSpec{}, err
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R03", name: "open-ended range",
			given: "sample.bin (1024 bytes)",
			when:  `Range: bytes=1000-`,
			then:  "206, bytes 1000-1023/1024, 24 bytes",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				rep, _ := c.FetchRepresentation(idSample, "")
				out, err := c.FetchRanges(idSample, "1000-", "", "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.StatusCode != 206 || out.ContentRange != "bytes 1000-1023/1024" {
					return report.ResponseSpec{}, failf("got %d %q", out.StatusCode, out.ContentRange)
				}
				if err := client.Verify(out.Single.Payload, rep.Body[1000:]); err != nil {
					return report.ResponseSpec{}, err
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R04", name: "suffix range",
			given: "sample.bin (1024 bytes)",
			when:  `Range: bytes=-200`,
			then:  "206, last 200 bytes: 824-1023/1024",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				rep, _ := c.FetchRepresentation(idSample, "")
				out, err := c.FetchRanges(idSample, "-200", "", "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.ContentRange != "bytes 824-1023/1024" {
					return report.ResponseSpec{}, failf("Content-Range %q", out.ContentRange)
				}
				if err := client.Verify(out.Single.Payload, rep.Body[824:]); err != nil {
					return report.ResponseSpec{}, err
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R05", name: "suffix longer than representation covers all",
			given: "sample.bin (1024 bytes)",
			when:  `Range: bytes=-5000`,
			then:  "206, bytes 0-1023/1024 (suffix clamped to full rep)",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				rep, _ := c.FetchRepresentation(idSample, "")
				out, err := c.FetchRanges(idSample, "-5000", "", "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.ContentRange != "bytes 0-1023/1024" {
					return report.ResponseSpec{}, failf("Content-Range %q", out.ContentRange)
				}
				if err := client.Verify(out.Single.Payload, rep.Body); err != nil {
					return report.ResponseSpec{}, err
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R06", name: "last-pos beyond end is clamped",
			given: "sample.bin (1024 bytes)",
			when:  `Range: bytes=900-5000`,
			then:  "206, bytes 900-1023/1024",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				rep, _ := c.FetchRepresentation(idSample, "")
				out, err := c.FetchRanges(idSample, "900-5000", "", "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.ContentRange != "bytes 900-1023/1024" {
					return report.ResponseSpec{}, failf("Content-Range %q", out.ContentRange)
				}
				if err := client.Verify(out.Single.Payload, rep.Body[900:]); err != nil {
					return report.ResponseSpec{}, err
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R07", name: "unsatisfiable: first-pos at size",
			given: "sample.bin (1024 bytes)",
			when:  `Range: bytes=1024-`,
			then:  "416 with Content-Range: bytes */1024",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				out, err := c.FetchRanges(idSample, "1024-", "", "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.StatusCode != http.StatusRequestedRangeNotSatisfiable {
					return report.ResponseSpec{}, failf("status %d", out.StatusCode)
				}
				if out.ContentRange != "bytes */1024" {
					return report.ResponseSpec{}, failf("Content-Range %q", out.ContentRange)
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R08", name: "unsatisfiable: entirely past end",
			given: "sample.bin (1024 bytes)",
			when:  `Range: bytes=2000-3000`,
			then:  "416, bytes */1024",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				out, err := c.FetchRanges(idSample, "2000-3000", "", "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.StatusCode != 416 || out.ContentRange != "bytes */1024" {
					return report.ResponseSpec{}, failf("got %d %q", out.StatusCode, out.ContentRange)
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R09", name: "zero-length artifact: plain GET",
			given: "empty.bin (0 bytes)",
			when:  "GET /artifacts/empty.bin",
			then:  "200 with Content-Length: 0",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				rep, err := c.FetchRepresentation(idEmpty, "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if len(rep.Body) != 0 {
					return report.ResponseSpec{}, failf("body not empty: %d", len(rep.Body))
				}
				if rep.Headers.Get("Content-Length") != "0" {
					return report.ResponseSpec{}, failf("Content-Length %q", rep.Headers.Get("Content-Length"))
				}
				return respSpec(http.StatusOK, rep.Headers, 0, rep.Body), nil
			},
		},
		{
			id: "R10", name: "zero-length artifact: any range is unsatisfiable",
			given: "empty.bin (0 bytes)",
			when:  `Range: bytes=0-0`,
			then:  "416 with Content-Range: bytes */0",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				out, err := c.FetchRanges(idEmpty, "0-0", "", "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.StatusCode != 416 || out.ContentRange != "bytes */0" {
					return report.ResponseSpec{}, failf("got %d %q", out.StatusCode, out.ContentRange)
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R11", name: "multiple ranges: exact partition reassembles to original",
			given: "sample.bin (1024 bytes)",
			when:  "Range: bytes=0-255,256-511,512-767,768-1023",
			then:  "206 multipart/byteranges; client reassembly byte-identical to original",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				rep, _ := c.FetchRepresentation(idSample, "")
				out, err := c.FetchRanges(idSample, "0-255,256-511,512-767,768-1023", "", "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.StatusCode != 206 || len(out.Parts) != 4 {
					return report.ResponseSpec{}, failf("want 4 multipart parts, got status %d parts %d",
						out.StatusCode, len(out.Parts))
				}
				got, err := client.Reassemble(out.Parts, int64(len(rep.Body)))
				if err != nil {
					return report.ResponseSpec{}, failf("reassembly: %v", err)
				}
				if err := client.Verify(got, rep.Body); err != nil {
					return report.ResponseSpec{}, err
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R12", name: "overlapping ranges delivered; strict reassembly rejects overlap",
			given: "sample.bin (1024 bytes)",
			when:  "Range: bytes=100-300,200-400",
			then:  "206 multipart with 2 parts, each byte-correct; Reassemble reports overlap",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				rep, _ := c.FetchRepresentation(idSample, "")
				out, err := c.FetchRanges(idSample, "100-300,200-400", "", "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if len(out.Parts) != 2 {
					return report.ResponseSpec{}, failf("want 2 parts, got %d", len(out.Parts))
				}
				cov, err := client.VerifyParts(out.Parts, rep.Body)
				if err != nil {
					return report.ResponseSpec{}, failf("part bytes wrong despite overlap: %v", err)
				}
				if cov.Complete() {
					return report.ResponseSpec{}, failf("partial cover unexpectedly complete")
				}
				if _, err := client.Reassemble(out.Parts, int64(len(rep.Body))); !errors.Is(err, client.ErrOverlap) {
					return report.ResponseSpec{}, failf("want ErrOverlap, got %v", err)
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R13", name: "range count at the limit is served",
			given: "range member limit = 5",
			when:  "Range with 5 members",
			then:  "206 multipart/byteranges",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				out, err := c.FetchRanges(idSample, "0-9,10-19,20-29,30-39,40-49", "", "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.StatusCode != 206 || len(out.Parts) != 5 {
					return report.ResponseSpec{}, failf("got status %d parts %d", out.StatusCode, len(out.Parts))
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R14", name: "range count over the limit is ignored (200 full)",
			given: "range member limit = 5",
			when:  "Range with 6 members",
			then:  "server ignores Range and returns 200 full representation",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				rep, _ := c.FetchRepresentation(idSample, "")
				out, err := c.FetchRanges(idSample, "0-1,2-3,4-5,6-7,8-9,10-11", "", "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.StatusCode != http.StatusOK {
					return report.ResponseSpec{}, failf("status %d", out.StatusCode)
				}
				if err := client.Verify(out.FullBody, rep.Body); err != nil {
					return report.ResponseSpec{}, err
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R15", name: "If-Range matching strong ETag allows 206",
			given: "current strong ETag",
			when:  "Range: bytes=0-49 with If-Range: <current ETag>",
			then:  "206 partial response",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				rep, _ := c.FetchRepresentation(idSample, "")
				out, err := c.FetchRanges(idSample, "0-49", rep.ETag, "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.StatusCode != 206 || out.Single == nil {
					return report.ResponseSpec{}, failf("status %d", out.StatusCode)
				}
				if err := client.Verify(out.Single.Payload, rep.Body[0:50]); err != nil {
					return report.ResponseSpec{}, err
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R16", name: "If-Range mismatched ETag returns full 200",
			given: "client holds a stale strong ETag",
			when:  "Range + If-Range with a non-matching strong ETag",
			then:  "200 full representation; no Content-Range",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				rep, _ := c.FetchRepresentation(idSample, "")
				out, err := c.FetchRanges(idSample, "0-49", `"00000000000000000000000000000000"`, "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.StatusCode != http.StatusOK {
					return report.ResponseSpec{}, failf("status %d", out.StatusCode)
				}
				if out.ContentRange != "" {
					return report.ResponseSpec{}, failf("unexpected Content-Range %q", out.ContentRange)
				}
				if err := client.Verify(out.FullBody, rep.Body); err != nil {
					return report.ResponseSpec{}, err
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R17", name: "If-Range weak ETag is ignored (206)",
			given: "If-Range carries W/\"...\"",
			when:  "Range + weak If-Range entity-tag",
			then:  "field ignored per RFC 9110; 206 served",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				out, err := c.FetchRanges(idSample, "0-9", `W/"weak"`, "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.StatusCode != 206 {
					return report.ResponseSpec{}, failf("status %d", out.StatusCode)
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R18", name: "If-Range date newer than Last-Modified allows 206",
			given: "Last-Modified 2026-01-15T12:00:00Z",
			when:  "If-Range dated one day later + Range",
			then:  "206 partial response",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				later := httpDate(fixtureTime.Add(24 * time.Hour))
				out, err := c.FetchRanges(idSample, "0-9", later, "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.StatusCode != 206 {
					return report.ResponseSpec{}, failf("status %d", out.StatusCode)
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R19", name: "If-Range stale date returns full 200",
			given: "Last-Modified 2026-01-15T12:00:00Z",
			when:  "If-Range dated one day earlier + Range",
			then:  "200 full representation",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				rep, _ := c.FetchRepresentation(idSample, "")
				earlier := httpDate(fixtureTime.Add(-24 * time.Hour))
				out, err := c.FetchRanges(idSample, "0-9", earlier, "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.StatusCode != http.StatusOK {
					return report.ResponseSpec{}, failf("status %d", out.StatusCode)
				}
				if err := client.Verify(out.FullBody, rep.Body); err != nil {
					return report.ResponseSpec{}, err
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R20", name: "malformed Range header is ignored",
			given: "sample.bin",
			when:  "Range: bytes=abc (syntactically invalid)",
			then:  "200 full representation",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				rep, _ := c.FetchRepresentation(idSample, "")
				out, err := c.FetchRangesRaw(idSample, "bytes=abc", nil)
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.StatusCode != http.StatusOK {
					return report.ResponseSpec{}, failf("status %d", out.StatusCode)
				}
				if err := client.Verify(out.FullBody, rep.Body); err != nil {
					return report.ResponseSpec{}, err
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R21", name: "unsupported range unit is ignored",
			given: "sample.bin",
			when:  "Range: items=0-9",
			then:  "200 full representation",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				rep, _ := c.FetchRepresentation(idSample, "")
				out, err := c.FetchRangesRaw(idSample, "items=0-9", nil)
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.StatusCode != http.StatusOK || !bytes.Equal(out.FullBody, rep.Body) {
					return report.ResponseSpec{}, failf("status %d", out.StatusCode)
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R22", name: "ranges index the selected (gzip) representation",
			given: "gzip-enabled origin; Accept-Encoding: gzip",
			when:  "full GET gzip, then Range: bytes=0- over the encoded bytes",
			then:  "206 reassembles to the encoded body; gzip ETag differs from identity",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.gzc()
				gz, err := c.FetchRepresentation(idSample, "gzip")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if gz.Encoding != "gzip" {
					return report.ResponseSpec{}, failf("not gzip encoded: %q", gz.Encoding)
				}
				ident, _ := e.c().FetchRepresentation(idSample, "")
				if gz.ETag == ident.ETag {
					return report.ResponseSpec{}, failf("gzip ETag identical to identity")
				}
				out, err := c.FetchRanges(idSample, "0-", gz.ETag, "gzip")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.StatusCode != 206 || out.Single == nil {
					return report.ResponseSpec{}, failf("status %d", out.StatusCode)
				}
				if err := client.Verify(out.Single.Payload, gz.Body); err != nil {
					return report.ResponseSpec{}, err
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "R23", name: "If-None-Match hit yields 304",
			given: "current ETag known to client",
			when:  "GET with If-None-Match: <ETag>",
			then:  "304 Not Modified, no body",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				rep, _ := c.FetchRepresentation(idSample, "")
				req, _ := http.NewRequest(http.MethodGet, e.identity.BaseURL+"/artifacts/"+idSample, nil)
				req.Header.Set("If-None-Match", rep.ETag)
				resp, err := http.DefaultClient.Do(req)
				if err != nil {
					return report.ResponseSpec{}, err
				}
				defer resp.Body.Close()
				body, _ := io.ReadAll(resp.Body)
				if resp.StatusCode != http.StatusNotModified || len(body) != 0 {
					return report.ResponseSpec{}, failf("status %d body %d", resp.StatusCode, len(body))
				}
				return respSpec(resp.StatusCode, resp.Header, resp.ContentLength, body), nil
			},
		},
		{
			id: "R24", name: "HEAD returns range metadata without a body",
			given: "sample.bin",
			when:  "HEAD with Range: bytes=0-9",
			then:  "206, correct Content-Range and Content-Length, empty body",
			run: func(e *env) (report.ResponseSpec, error) {
				req, _ := http.NewRequest(http.MethodHead, e.identity.BaseURL+"/artifacts/"+idSample, nil)
				req.Header.Set("Range", "bytes=0-9")
				resp, err := http.DefaultClient.Do(req)
				if err != nil {
					return report.ResponseSpec{}, err
				}
				defer resp.Body.Close()
				body, _ := io.ReadAll(resp.Body)
				if resp.StatusCode != 206 {
					return report.ResponseSpec{}, failf("status %d", resp.StatusCode)
				}
				if resp.Header.Get("Content-Range") != "bytes 0-9/1024" || len(body) != 0 {
					return report.ResponseSpec{}, failf("CR=%q body=%d", resp.Header.Get("Content-Range"), len(body))
				}
				return respSpec(resp.StatusCode, resp.Header, resp.ContentLength, body), nil
			},
		},
		{
			id: "F01", name: "fault: truncated body is detected",
			given: "transport truncates after 64 of 1024 bytes",
			when:  "plain GET through truncating transport",
			then:  "client rejects with ErrTruncated (Content-Length mismatch)",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				c.SetFault(fault.Config{Kind: fault.KindTruncate, AtByte: 64})
				_, err := c.FetchRepresentation(idSample, "")
				if !errors.Is(err, client.ErrTruncated) {
					return report.ResponseSpec{}, failf("want ErrTruncated, got %v", err)
				}
				return report.ResponseSpec{StatusCode: 200, BodyLength: 64}, nil
			},
		},
		{
			id: "F02", name: "fault: abrupt abort is detected",
			given: "transport aborts after 64 bytes (unexpected EOF)",
			when:  "plain GET through aborting transport",
			then:  "client surfaces the read error (no silent partial body)",
			run: func(e *env) (report.ResponseSpec, error) {
				c := e.c()
				c.SetFault(fault.Config{Kind: fault.KindAbort, AtByte: 64})
				_, err := c.FetchRepresentation(idSample, "")
				if err == nil {
					return report.ResponseSpec{}, failf("expected read error, got nil")
				}
				return report.ResponseSpec{StatusCode: 200, BodyLength: 64}, nil
			},
		},
		{
			id: "F03", name: "fault: corrupted byte fails end-to-end verification",
			given: "transport flips byte 10 of a range response",
			when:  "Range: bytes=0-99 through corrupting transport; verify vs ground truth",
			then:  "Verify reports integrity failure",
			run: func(e *env) (report.ResponseSpec, error) {
				good := e.c()
				rep, _ := good.FetchRepresentation(idSample, "")

				bad := e.c()
				bad.SetFault(fault.Config{Kind: fault.KindCorrupt, AtByte: 10})
				out, err := bad.FetchRanges(idSample, "0-99", "", "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if err := client.Verify(out.Single.Payload, rep.Body[0:100]); !errors.Is(err, client.ErrIntegrity) {
					return report.ResponseSpec{}, failf("want ErrIntegrity, got %v", err)
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
		{
			id: "F04", name: "fault: ETag stripped does not fool byte verification",
			given: "transport removes ETag from a 206",
			when:  "Range: bytes=0-99; client has independent ground truth",
			then:  "206 body still byte-verified against a separately fetched copy",
			run: func(e *env) (report.ResponseSpec, error) {
				good := e.c()
				rep, _ := good.FetchRepresentation(idSample, "")

				stripped := e.c()
				stripped.SetFault(fault.Config{Kind: fault.KindStripETag})
				out, err := stripped.FetchRanges(idSample, "0-99", "", "")
				if err != nil {
					return report.ResponseSpec{}, err
				}
				if out.ETag != "" {
					return report.ResponseSpec{}, failf("ETag not stripped: %q", out.ETag)
				}
				if err := client.Verify(out.Single.Payload, rep.Body[0:100]); err != nil {
					return report.ResponseSpec{}, failf("bytes wrong: %v", err)
				}
				return respSpec(out.StatusCode, out.Headers, out.ContentLength, out.Body), nil
			},
		},
	}
}
