package integration

import (
	"net"
	"os"
	"strconv"
	"testing"
)

func slowTestsEnabled() string { return os.Getenv("RUN_SLOW_TESTS") }

func ptrString(s string) *string { return &s }

// freeLocalPort returns a TCP port that was free at call time. There is a
// small TOCTOU window, acceptable for a test-only second webhook listener.
func freeLocalPort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("allocate port: %v", err)
	}
	addr := l.Addr().(*net.TCPAddr)
	_ = l.Close()
	return addr.Port
}

func netJoinHostPort(host string, port int) string {
	return net.JoinHostPort(host, strconv.Itoa(port))
}
