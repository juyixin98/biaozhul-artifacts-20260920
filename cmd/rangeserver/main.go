// Command rangeserver runs the local HTTP range service.
//
// It seeds a handful of deterministic, in-memory artifacts (all external
// dependencies are in-process fakes) and serves the RFC 9110 byte-range
// API. Nothing here connects to a production system.
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/example/rangeserver/internal/artifact"
	"github.com/example/rangeserver/internal/server"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	flag.Parse()

	reg := newRegistry()
	srv := &http.Server{
		Addr:              *addr,
		Handler:           server.New(reg),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("range service listening on http://%s (artifacts: %s)", *addr, strings.Join(reg.IDs(), ", "))
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("server error: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("shutdown error: %v", err)
	}
}

// newRegistry seeds deterministic artifacts used by the examples and by
// curl-based manual checks.
func newRegistry() *artifact.Registry {
	reg := artifact.NewRegistry()

	// 256-byte binary: bytes 0..255, an obvious fixture for offsets.
	binary256 := make([]byte, 256)
	for i := range binary256 {
		binary256[i] = byte(i)
	}
	reg.Put(artifact.NewWithMetadata("binary256", binary256, time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)))

	// Repeating text, large enough for suffix/overlap demos.
	text := strings.Repeat("ABCDEFGHIJ", 30) // 300 bytes
	reg.Put(artifact.NewWithMetadata("lorem300", []byte(text), time.Date(2026, 1, 2, 3, 4, 6, 0, time.UTC)))

	// Zero-length artifact, required by the acceptance criteria.
	reg.Put(artifact.NewWithMetadata("empty", []byte{}, time.Date(2026, 1, 2, 3, 4, 7, 0, time.UTC)))

	return reg
}
