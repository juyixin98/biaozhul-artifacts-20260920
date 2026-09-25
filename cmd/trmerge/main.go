// Command trmerge is the local test-result merging service.
//
// It is a pure backend: JSON over HTTP, no cloud connection. The cache
// directory (event logs) and the work directory (where explicit fixture
// commands run) are kept strictly separate.
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
	"path/filepath"
	"syscall"
	"time"

	"trmerge"
)

func main() {
	var (
		addr      = flag.String("addr", "127.0.0.1:8080", "listen address")
		cacheRoot = flag.String("cache", "", "cache directory for event logs (default ./.trmerge-cache)")
		workRoot  = flag.String("work", "", "work directory for fixture commands (default ./.trmerge-work)")
	)
	flag.Parse()

	cwd, err := os.Getwd()
	if err != nil {
		log.Fatal(err)
	}
	if *cacheRoot == "" {
		*cacheRoot = filepath.Join(cwd, ".trmerge-cache")
	}
	if *workRoot == "" {
		*workRoot = filepath.Join(cwd, ".trmerge-work")
	}

	store, err := trmerge.NewStore(*cacheRoot)
	if err != nil {
		log.Fatalf("open cache: %v", err)
	}
	srv, err := trmerge.NewServer(store, *workRoot)
	if err != nil {
		log.Fatalf("server: %v", err)
	}

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("trmerge listening on http://%s", *addr)
		log.Printf("  cache (event logs): %s", store.CacheRoot())
		log.Printf("  work  (fixtures):   %s", *workRoot)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	log.Printf("shutting down…")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(ctx); err != nil {
		log.Printf("shutdown: %v", err)
	}
	fmt.Println("bye")
}
