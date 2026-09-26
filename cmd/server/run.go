package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"time"

	"cancelprop/internal/clock"
	"cancelprop/internal/fakesvc"
	"cancelprop/internal/serverapi"
)

// run wires and serves the full stack until ctx is canceled, then performs
// an orderly shutdown and returns nil. It is the testable core of main.
func run(ctx context.Context, addr string) error {
	svcA := fakesvc.New("a")
	svcB := fakesvc.New("b")
	if err := svcA.Start(); err != nil {
		return err
	}
	if err := svcB.Start(); err != nil {
		return err
	}

	app := serverapi.New(serverapi.Config{
		Clock:    clock.NewRealClock(),
		Services: []*fakesvc.Server{svcA, svcB},
	})
	srv := &http.Server{
		Addr:              addr,
		Handler:           app.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	serveErr := make(chan error, 1)
	go func() {
		log.Printf("cancelprop server listening on http://%s", addr)
		log.Printf("fake downstreams: a=%s b=%s", svcA.URL(), svcB.URL())
		err := srv.ListenAndServe()
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			serveErr <- err
			return
		}
		serveErr <- nil
	}()

	// Return promptly either when the caller cancels (normal shutdown) or
	// when the listener dies on its own (e.g. address already in use).
	var serveFailErr error
	select {
	case <-ctx.Done():
		log.Println("shutdown signal received")
	case err := <-serveErr:
		serveFailErr = err
		log.Printf("server stopped serving: %v", err)
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("app shutdown: %v", err)
	}
	app.CloseIdleConnections()
	if err := svcA.Shutdown(shutdownCtx); err != nil {
		log.Printf("fake a shutdown: %v", err)
	}
	if err := svcB.Shutdown(shutdownCtx); err != nil {
		log.Printf("fake b shutdown: %v", err)
	}
	log.Println("stopped cleanly")
	if serveFailErr != nil {
		return serveFailErr
	}
	return <-serveErr
}
