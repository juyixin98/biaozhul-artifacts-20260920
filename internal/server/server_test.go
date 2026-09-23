package server

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"logpipe/internal/merger"
	"logpipe/internal/store"
)

func newTestServer(t *testing.T, timeout time.Duration, maxBytes int, sweep time.Duration) (*httptest.Server, func()) {
	t.Helper()
	st, err := store.Open(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatalf("store: %v", err)
	}
	srv := New(Config{
		StartRule:     regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T`),
		Timeout:       timeout,
		MaxBytes:      maxBytes,
		SweepInterval: sweep,
	}, st, log.New(io.Discard, "", 0))
	ts := httptest.NewServer(srv.Handler())
	return ts, func() {
		ts.Close()
		srv.Close()
		_ = st.Close()
	}
}

func doIngest(t *testing.T, url string, body any) {
	t.Helper()
	data, _ := json.Marshal(body)
	resp, err := http.Post(url+"/ingest", "application/json", bytes.NewReader(data))
	if err != nil {
		t.Fatalf("ingest: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusAccepted {
		data, _ := io.ReadAll(resp.Body)
		t.Fatalf("ingest status = %s body=%s", resp.Status, data)
	}
}

type queryResult struct {
	Entries []merger.Entry `json:"entries"`
	Count   int            `json:"count"`
}

func query(t *testing.T, url, source string) queryResult {
	t.Helper()
	q := url + "/entries"
	if source != "" {
		q += "?source=" + source
	}
	resp, err := http.Get(q)
	if err != nil {
		t.Fatalf("query: %v", err)
	}
	defer resp.Body.Close()
	var out queryResult
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return out
}

func TestIngestSingleAndBatchValidation(t *testing.T) {
	ts, cleanup := newTestServer(t, time.Minute, 4096, 0)
	defer cleanup()

	// Single object.
	doIngest(t, ts.URL, map[string]string{"source": "s", "text": "2026-09-24T10:00:00 h"})
	// Batch.
	doIngest(t, ts.URL, []map[string]any{
		{"source": "s", "text": "  f1"},
		{"source": "s", "text": "  f2"},
	})

	// Empty source rejected.
	data, _ := json.Marshal(map[string]string{"source": " ", "text": "x"})
	resp, err := http.Post(ts.URL+"/ingest", "application/json", bytes.NewReader(data))
	if err != nil {
		t.Fatalf("ingest: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("bad source status = %d, want 400", resp.StatusCode)
	}

	// Malformed JSON rejected.
	resp, err = http.Post(ts.URL+"/ingest", "application/json", strings.NewReader("{oops"))
	if err != nil {
		t.Fatalf("ingest: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("bad json status = %d, want 400", resp.StatusCode)
	}
}

func TestForceFlushAssemblesAndPersists(t *testing.T) {
	ts, cleanup := newTestServer(t, time.Minute, 4096, 0)
	defer cleanup()

	doIngest(t, ts.URL, []map[string]any{
		{"source": "svc-a", "text": "2026-09-24T10:00:00 header A"},
		{"source": "svc-a", "text": "    frame A1"},
	})

	resp, err := http.Post(ts.URL+"/admin/force-flush", "application/json", nil)
	if err != nil {
		t.Fatalf("force-flush: %v", err)
	}
	var fr map[string]int
	json.NewDecoder(resp.Body).Decode(&fr)
	resp.Body.Close()
	if fr["flushed"] != 1 {
		t.Fatalf("flushed = %v, want 1", fr)
	}

	res := query(t, ts.URL, "svc-a")
	if res.Count != 1 {
		t.Fatalf("count = %d, want 1", res.Count)
	}
	e := res.Entries[0]
	if e.LineCount != 2 || e.Complete || e.Reason != merger.ReasonForced {
		t.Fatalf("entry = %+v", e)
	}
}

func TestTimeoutSweepOverHTTP(t *testing.T) {
	ts, cleanup := newTestServer(t, 60*time.Millisecond, 4096, 20*time.Millisecond)
	defer cleanup()

	doIngest(t, ts.URL, []map[string]any{
		{"source": "svc-t", "text": "2026-09-24T10:00:00 header"},
		{"source": "svc-t", "text": "    frame"},
	})

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if query(t, ts.URL, "svc-t").Count == 1 {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	res := query(t, ts.URL, "svc-t")
	if res.Count != 1 {
		t.Fatalf("entry not flushed by timeout sweep, count=%d", res.Count)
	}
	e := res.Entries[0]
	if !e.Complete || e.Reason != merger.ReasonTimeout {
		t.Fatalf("entry = %+v, want complete timeout", e)
	}
}

func TestInterleavedSourcesOverHTTP(t *testing.T) {
	ts, cleanup := newTestServer(t, time.Minute, 4096, 0)
	defer cleanup()

	doIngest(t, ts.URL, []map[string]any{
		{"source": "alpha", "text": "2026-09-24T10:00:00 A-start"},
		{"source": "beta", "text": "2026-09-24T10:00:00 B-start"},
		{"source": "alpha", "text": "    A-frame"},
		{"source": "beta", "text": "    B-frame"},
	})
	resp, _ := http.Post(ts.URL+"/admin/force-flush", "application/json", nil)
	resp.Body.Close()

	all := query(t, ts.URL, "")
	if all.Count != 2 {
		t.Fatalf("count = %d, want 2", all.Count)
	}
	for _, e := range all.Entries {
		if strings.Contains(e.Text, "A-") && strings.Contains(e.Text, "B-") {
			t.Fatalf("cross-source mixing in entry: %q", e.Text)
		}
		if e.LineCount != 2 {
			t.Fatalf("entry %q lines = %d, want 2", e.Source, e.LineCount)
		}
	}
	if got := query(t, ts.URL, "alpha"); got.Count != 1 || !strings.Contains(got.Entries[0].Text, "A-frame") {
		t.Fatalf("source filter broken: %+v", got)
	}
}

func TestHealth(t *testing.T) {
	ts, cleanup := newTestServer(t, time.Minute, 4096, 0)
	defer cleanup()
	resp, err := http.Get(ts.URL + "/healthz")
	if err != nil {
		t.Fatalf("health: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
}
