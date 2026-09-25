package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"deltaupdate/internal/server"
)

func runServe(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	addr := fs.String("addr", "127.0.0.1:8080", "listen address (local only)")
	cache := fs.String("cache", "", "cache directory")
	work := fs.String("work", "", "working directory (targets)")
	maxUp := fs.Int64("max-upload-bytes", 0, "max request body bytes")
	if err := fs.Parse(args); err != nil {
		return err
	}

	srv, err := server.New(server.Config{
		CacheDir:       *cache,
		WorkDir:        *work,
		MaxUploadBytes: *maxUp,
	})
	if err != nil {
		return err
	}
	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	errCh := make(chan error, 1)
	go func() {
		log.Printf("delta-update serve on %s (cache=%s work=%s)", *addr, *cache, *work)
		errCh <- httpSrv.ListenAndServe()
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	select {
	case err := <-errCh:
		if err != http.ErrServerClosed {
			return err
		}
		return nil
	case sig := <-sigCh:
		log.Printf("received %s, shutting down", sig)
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return httpSrv.Shutdown(ctx)
	}
}
