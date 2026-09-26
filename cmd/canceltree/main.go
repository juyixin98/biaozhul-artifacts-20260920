// Command canceltree runs the local, self-contained request-cancellation
// demo service: HTTP API, fault-injecting client and controllable fake
// upstream all live in one process. Nothing here talks to a production
// system.
package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"canceltree/internal/server"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	attempts := flag.Int("attempts", 1, "default client attempts per task (>=1)")
	flag.Parse()

	if *attempts < 1 {
		*attempts = 1
	}

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	if err := run(*addr, *attempts, sigCh); err != nil {
		log.Fatalf("server error: %v", err)
	}
	log.Println("stopped")
}

// run starts the HTTP server and blocks until shutdown fires or the listener
// fails. The signal channel is injectable so tests can trigger a graceful
// stop without sending real process signals.
func run(addr string, attempts int, shutdown <-chan os.Signal) error {
	log.Printf("canceltree listening on http://%s (fake upstream mounted at /upstream)", addr)

	srv := server.New(server.Options{Addr: addr, ClientAttempts: attempts})

	errCh := make(chan error, 1)
	go func() { errCh <- srv.ListenAndServe() }()

	select {
	case err := <-errCh:
		return err
	case sig := <-shutdown:
		log.Printf("received signal %s, shutting down", sig)
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return srv.Shutdown(ctx)
	}
}
