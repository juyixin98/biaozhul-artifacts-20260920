package origin

import (
	"context"
	"io"
	"net/http"
	"testing"
	"time"

	"httprange/internal/artifact"
	"httprange/internal/clock"
)

func TestStartAndServe(t *testing.T) {
	clk := clock.NewFake(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	store := artifact.NewStore().Put(artifact.New("a.bin",
		[]byte("0123456789"), "application/octet-stream", clk.Now()))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	h, err := Start(ctx, Config{Store: store, Clock: clk, MaxRanges: 3})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = h.Shutdown() }()

	resp, err := http.Get(h.BaseURL + "/artifacts/a.bin")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if string(body) != "0123456789" {
		t.Fatalf("body %q", body)
	}
	if resp.Header.Get("Accept-Ranges") != "bytes" {
		t.Fatal("missing Accept-Ranges")
	}

	// Range member limit is exposed and enforced.
	req, _ := http.NewRequest(http.MethodGet, h.BaseURL+"/artifacts/a.bin", nil)
	req.Header.Set("Range", "bytes=0-1,2-3,4-5,6-7")
	resp2, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusOK {
		t.Fatalf("over-limit range set: status %d, want 200", resp2.StatusCode)
	}
}

func TestStartGzipVariant(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	store := artifact.NewStore().Put(artifact.New("a", []byte("payload"),
		"application/octet-stream", time.Now()))
	h, err := Start(ctx, Config{Store: store, EnableGzip: true})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = h.Shutdown() }()

	req, _ := http.NewRequest(http.MethodGet, h.BaseURL+"/artifacts/a", nil)
	req.Header.Set("Accept-Encoding", "gzip")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.Header.Get("Content-Encoding") != "gzip" {
		t.Fatalf("encoding %q", resp.Header.Get("Content-Encoding"))
	}
}
