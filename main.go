// Command loopmail runs a loopback-only SMTP test receiver and a small HTTP
// API over the messages it received. Nothing is ever delivered or relayed:
// fully received messages are only stored on the local filesystem.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"loopmail/httpapi"
	"loopmail/smtpd"
	"loopmail/store"
)

func main() {
	smtpAddr := flag.String("smtp-addr", "127.0.0.1:2525", "loopback SMTP listen address (host must be 127.0.0.1/::1)")
	httpAddr := flag.String("http-addr", "127.0.0.1:8080", "loopback HTTP API listen address")
	dataDir := flag.String("data-dir", "./data", "directory used for the message spool")
	maxSize := flag.Int("max-size-kb", 1024, "maximum accepted DATA payload in KiB")
	idleSecs := flag.Int("idle-timeout-sec", 120, "per-command/data-line idle timeout in seconds")
	flag.Parse()

	log := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))

	// Refuse any non-loopback HTTP bind up front, mirroring the SMTP guard.
	if err := smtpd.ValidateLoopbackAddr(*httpAddr); err != nil {
		fatal(err)
	}

	st, err := store.Open(*dataDir)
	if err != nil {
		fatal(err)
	}

	srv, err := smtpd.New(smtpd.Config{
		Addr:            *smtpAddr,
		Hostname:        "loopmail.local",
		MaxMessageBytes: *maxSize * 1024,
		IdleTimeout:     time.Duration(*idleSecs) * time.Second,
		Sink:            st,
		Logger:          log,
	})
	if err != nil {
		fatal(err)
	}

	httpSrv := &http.Server{
		Addr:              *httpAddr,
		Handler:           httpapi.Handler(st, log),
		ReadHeaderTimeout: 10 * time.Second,
	}

	smtpErr := make(chan error, 1)
	go func() {
		log.Info("SMTP receiver listening (loopback only, no outbound delivery)", "addr", *smtpAddr, "max_kib", *maxSize)
		smtpErr <- srv.ListenAndServe()
	}()

	httpErr := make(chan error, 1)
	go func() {
		log.Info("HTTP API listening (loopback only)", "addr", *httpAddr)
		httpErr <- httpSrv.ListenAndServe()
	}()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	select {
	case <-ctx.Done():
		log.Info("shutdown signal received")
	case err := <-smtpErr:
		if !errors.Is(err, smtpd.ErrServerClosed) {
			log.Error("SMTP server error", "err", err)
		}
	case err := <-httpErr:
		if !errors.Is(err, http.ErrServerClosed) {
			log.Error("HTTP server error", "err", err)
		}
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Error("SMTP shutdown error", "err", err)
	}
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		log.Error("HTTP shutdown error", "err", err)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "loopmail:", err)
	os.Exit(1)
}
