package server

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"testing"
	"time"

	"streambp/internal/client"
	"streambp/internal/clock"
)

func TestStatsAndHealthEndpoints(t *testing.T) {
	_, ts := newTestServer(t, baseConfig())

	resp, err := http.Get(ts.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("healthz: got %d", resp.StatusCode)
	}

	resp, err = http.Get(ts.URL + "/stats")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var st Stats
	if err := json.NewDecoder(resp.Body).Decode(&st); err != nil {
		t.Fatalf("stats decode: %v", err)
	}
	if st.BufferCapacity != 4 || st.MaxItemBytes != 1024 {
		t.Fatalf("stats mismatch: %+v", st)
	}
}

func TestListenAndServeGracefulLifecycle(t *testing.T) {
	// Grab a free port, release it, and hand the address to the server.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	ln.Close()

	srv, err := New(baseConfig(), clock.Real{})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- srv.ListenAndServe(ctx, addr) }()

	// Wait for the listener to come up, then stream to completion.
	var res client.Result
	up := false
	for i := 0; i < 100; i++ {
		res = client.Run(context.Background(), client.Options{
			URL:     fmt.Sprintf("http://%s/stream?items=5", addr),
			Timeout: 2 * time.Second,
		})
		if res.Failure == "" {
			up = true
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !up {
		t.Fatalf("server never came up: %+v", res)
	}
	if res.Received != 5 || res.EndStatus != "ok" {
		t.Fatalf("unexpected result: %+v", res)
	}

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("ListenAndServe: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("ListenAndServe did not return after cancel")
	}

	// The listener must be closed now.
	if _, err := http.Get(fmt.Sprintf("http://%s/healthz", addr)); err == nil {
		t.Fatal("server still accepting after shutdown")
	} else if _, ok := err.(net.Error); !ok && err != io.EOF {
		// Any connection failure is fine; just log unexpected kinds.
		t.Logf("post-shutdown request error (expected): %v", err)
	}
}
