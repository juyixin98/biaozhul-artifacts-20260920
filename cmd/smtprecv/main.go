// Command smtprecv runs a loopback-only SMTP test receiver together with a
// loopback-only HTTP query API. Nothing received here is ever relayed or
// delivered outbound.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"smtprecv/internal/httpapi"
	"smtprecv/internal/smtpd"
	"smtprecv/internal/store"
)

func main() {
	smtpAddr := flag.String("smtp-addr", "127.0.0.1:2525", "loopback address for the SMTP receiver")
	httpAddr := flag.String("http-addr", "127.0.0.1:8080", "loopback address for the HTTP API")
	dataDir := flag.String("data-dir", "./smtpdata", "directory for stored messages")
	hostname := flag.String("hostname", "localhost", "hostname advertised in SMTP greetings")
	maxSize := flag.Int("max-size", 1<<20, "maximum accepted message body size in bytes (default 1 MiB)")
	maxRcpt := flag.Int("max-recipients", 100, "maximum accepted recipients per message")
	idleTimeout := flag.Duration("idle-timeout", 2*time.Minute, "SMTP idle connection timeout")
	flag.Parse()

	if err := httpapi.RequireLoopback(*httpAddr); err != nil {
		log.Fatalf("HTTP config error: %v", err)
	}

	st, err := store.Open(*dataDir)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	log.Printf("store ready: %d message(s) in %s", len(st.List(0)), *dataDir)

	srv, err := smtpd.New(smtpd.Config{
		ListenAddr:     *smtpAddr,
		Hostname:       *hostname,
		MaxMessageSize: *maxSize,
		MaxRecipients:  *maxRcpt,
		CommandTimeout: *idleTimeout,
	}, st)
	if err != nil {
		log.Fatalf("SMTP config error: %v", err)
	}
	if err := srv.Start(); err != nil {
		log.Fatalf("start SMTP server: %v", err)
	}
	log.Printf("SMTP receiver listening on %s (loopback only, size limit %d bytes)", srv.Addr(), *maxSize)

	httpSrv := &http.Server{
		Addr:              *httpAddr,
		Handler:           httpapi.NewHandler(st),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("HTTP server: %v", err)
		}
	}()
	log.Printf("HTTP API listening on %s (loopback only)", *httpAddr)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	<-ctx.Done()
	log.Println("shutting down...")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		log.Printf("HTTP shutdown: %v", err)
	}
	if err := srv.Close(); err != nil {
		log.Printf("SMTP shutdown: %v", err)
	}
	fmt.Fprintln(os.Stderr, "bye")
}
