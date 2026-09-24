package testenv

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

// testWriter routes zap logs into t.Log.
type testWriter struct{ t *testing.T }

func (w *testWriter) Write(p []byte) (int, error) {
	w.t.Log(strings.TrimRight(string(p), "\n"))
	return len(p), nil
}

// freePort asks the kernel for an unused TCP port.
func freePort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("allocating free port: %v", err)
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port
}

// waitReady polls an endpoint with a client trusting the test CA until a
// TLS connection succeeds (HTTP status code is irrelevant; an empty POST
// still proves the server is accepting TLS connections).
func waitReady(rawURL string, caPEM []byte, timeout time.Duration) error {
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		return fmt.Errorf("failed to append test CA")
	}
	client := &http.Client{
		Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool}},
		Timeout:   time.Second,
	}
	deadline := time.Now().Add(timeout)
	var lastErr error
	for time.Now().Before(deadline) {
		req, reqErr := http.NewRequestWithContext(context.Background(), http.MethodPost, rawURL, nil)
		if reqErr != nil {
			lastErr = reqErr
		} else {
			resp, err := client.Do(req)
			if err == nil {
				resp.Body.Close()
				return nil
			}
			lastErr = err
		}
		time.Sleep(100 * time.Millisecond)
	}
	return fmt.Errorf("webhook at %s not ready within %s: %w", rawURL, timeout, lastErr)
}
