// Command server runs the conditional-update HTTP service on localhost.
//
// It is a local demo only: the "external" audit dependency is an in-process
// fake and state is in-memory. Use -addr to pick the listen address.
//
//	go run ./cmd/server -addr 127.0.0.1:8080
package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"etagrace/internal/clock"
	"etagrace/internal/notifier"
	"etagrace/internal/server"
	"etagrace/internal/store"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	flag.Parse()

	clk := clock.Real{}
	audit := notifier.NewAuditLog()
	// Standalone mode uses the reliable fake (no injected faults). The retry
	// wrapper is still in place for uniformity.
	notif := server.RetryingNotifier{
		Inner:       notifier.NewReliable(audit),
		Clk:         clk,
		Attempts:    3,
		BaseBackoff: 10 * time.Millisecond,
		MaxBackoff:  100 * time.Millisecond,
	}
	st := store.New(clk)
	srv := server.New(st, clk, notif, audit)

	httpd := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("conditional-update service listening on http://%s (in-memory, Ctrl-C to stop)", *addr)
	if err := httpd.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
