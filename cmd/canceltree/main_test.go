package main

import (
	"net"
	"net/http"
	"os"
	"syscall"
	"testing"
	"time"
)

func freePort(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	_ = l.Close()
	return addr
}

// TestRunServesAndShutsDownOnSignal exercises the full command wiring:
// listener, healthz, embedded fake upstream and graceful shutdown.
func TestRunServesAndShutsDownOnSignal(t *testing.T) {
	addr := freePort(t)
	stop := make(chan os.Signal, 1)
	errCh := make(chan error, 1)
	go func() { errCh <- run(addr, 1, stop) }()

	base := "http://" + addr
	deadline := time.Now().Add(3 * time.Second)
	var lastErr error
	for time.Now().Before(deadline) {
		resp, err := http.Get(base + "/healthz")
		if err == nil {
			_ = resp.Body.Close()
			lastErr = nil
			break
		}
		lastErr = err
		time.Sleep(10 * time.Millisecond)
	}
	if lastErr != nil {
		t.Fatalf("server never became healthy: %v", lastErr)
	}

	resp, err := http.Get(base + "/upstream/work?delay=1ms")
	if err != nil {
		t.Fatalf("fake upstream not reachable through main mux: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("upstream status = %d", resp.StatusCode)
	}

	stop <- syscall.SIGTERM
	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("graceful shutdown error: %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("run did not return after SIGTERM")
	}
}

func TestRunListenerFailureReported(t *testing.T) {
	// Binding the same address twice must surface the listen error.
	addr := freePort(t)
	l, err := net.Listen("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()

	stop := make(chan os.Signal, 1)
	if err := run(addr, 1, stop); err == nil {
		t.Fatal("expected listener error when the port is already taken")
	}
}
