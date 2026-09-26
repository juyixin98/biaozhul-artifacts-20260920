package main

import (
	"context"
	"net"
	"net/http"
	"testing"
	"time"
)

func freePort(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()
	return addr
}

func TestRun_ServesAndShutsDown(t *testing.T) {
	addr := freePort(t)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- run(ctx, addr) }()

	base := "http://" + addr
	deadline := time.Now().Add(3 * time.Second)
	var lastErr error
	for time.Now().Before(deadline) {
		resp, err := http.Get(base + "/healthz")
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				lastErr = nil
				break
			}
		}
		lastErr = err
		time.Sleep(10 * time.Millisecond)
	}
	if lastErr != nil {
		t.Fatalf("server never became healthy: %v", lastErr)
	}

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("run returned error: %v", err)
		}
	case <-time.After(6 * time.Second):
		t.Fatal("run did not return after shutdown")
	}
}

func TestRun_ListenerFailureReturns(t *testing.T) {
	// Occupy an address, then ask run() to serve on it: ListenAndServe
	// must fail and run must return that error rather than hanging.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = ln.Close() }()
	addr := ln.Addr().String()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan error, 1)
	go func() { done <- run(ctx, addr) }()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("expected ListenAndServe error on occupied address, got nil")
		}
	case <-time.After(6 * time.Second):
		t.Fatal("run hung instead of returning the listener error")
	}
}
