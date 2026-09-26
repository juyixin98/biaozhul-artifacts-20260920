package client

import (
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const sampleBody = "0123456789abcdefghij" // 20 bytes

func rangeTestServer() *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("ETag", `"v1"`)
		w.Header().Set("Accept-Ranges", "bytes")
		switch r.Header.Get("Range") {
		case "bytes=0-4":
			w.Header().Set("Content-Range", "bytes 0-4/20")
			w.WriteHeader(http.StatusPartialContent)
			_, _ = io.WriteString(w, sampleBody[0:5])
		case "bytes=0-4,15-19":
			w.Header().Set("Content-Type", "multipart/byteranges; boundary=B")
			w.WriteHeader(http.StatusPartialContent)
			_, _ = io.WriteString(w,
				"--B\r\nContent-Range: bytes 0-4/20\r\n\r\n"+sampleBody[0:5]+"\r\n"+
					"--B\r\nContent-Range: bytes 15-19/20\r\n\r\n"+sampleBody[15:20]+"\r\n"+
					"--B--\r\n")
		case "bytes=100-":
			w.Header().Set("Content-Range", "bytes */20")
			w.WriteHeader(http.StatusRequestedRangeNotSatisfiable)
		default:
			_, _ = io.WriteString(w, sampleBody)
		}
	}))
}

func TestFetchRepresentation(t *testing.T) {
	srv := rangeTestServer()
	defer srv.Close()
	c := New(srv.URL)
	rep, err := c.FetchRepresentation("x", "")
	if err != nil {
		t.Fatal(err)
	}
	if string(rep.Body) != sampleBody || rep.ETag != `"v1"` {
		t.Fatalf("body=%q etag=%q", rep.Body, rep.ETag)
	}
}

func TestFetchSingleRange(t *testing.T) {
	srv := rangeTestServer()
	defer srv.Close()
	out, err := New(srv.URL).FetchRanges("x", "0-4", "", "")
	if err != nil {
		t.Fatal(err)
	}
	if out.StatusCode != 206 || out.Single == nil {
		t.Fatalf("status %d", out.StatusCode)
	}
	if string(out.Single.Payload) != "01234" || out.Single.Total != 20 {
		t.Fatalf("part %+v", out.Single)
	}
}

func TestFetchMultipart(t *testing.T) {
	srv := rangeTestServer()
	defer srv.Close()
	out, err := New(srv.URL).FetchRanges("x", "0-4,15-19", "", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(out.Parts) != 2 {
		t.Fatalf("parts %d", len(out.Parts))
	}
	if string(out.Parts[1].Payload) != "fghij" {
		t.Fatalf("second part %q", out.Parts[1].Payload)
	}
}

func TestFetch416(t *testing.T) {
	srv := rangeTestServer()
	defer srv.Close()
	out, err := New(srv.URL).FetchRanges("x", "100-", "", "")
	if err != nil {
		t.Fatal(err)
	}
	if out.StatusCode != http.StatusRequestedRangeNotSatisfiable || out.ContentRange != "bytes */20" {
		t.Fatalf("status %d CR %q", out.StatusCode, out.ContentRange)
	}
}

func TestReadCheckedDetectsTruncation(t *testing.T) {
	// Mimic the fault transport: a body that ends cleanly after fewer bytes
	// than Content-Length declares.
	resp := &http.Response{
		ContentLength: 20,
		Body:          io.NopCloser(strings.NewReader("short")),
	}
	if _, err := readChecked(resp); !errors.Is(err, ErrTruncated) {
		t.Fatalf("want ErrTruncated, got %v", err)
	}
}

func TestBoundaryFromContentTypeErrors(t *testing.T) {
	if _, err := boundaryFromContentType("text/plain"); err == nil {
		t.Error("non-multipart must error")
	}
	if _, err := boundaryFromContentType("multipart/byteranges"); err == nil {
		t.Error("missing boundary must error")
	}
	if b, err := boundaryFromContentType(`multipart/byteranges; boundary="x"`); err != nil || b != "x" {
		t.Errorf("quoted boundary: %q %v", b, err)
	}
}

func TestParseMultipartGarbage(t *testing.T) {
	_, err := parseMultipart([]byte("not a multipart body"), "multipart/byteranges; boundary=B")
	if err == nil || !strings.Contains(err.Error(), "malformed") {
		t.Fatalf("want malformed error, got %v", err)
	}
}
