// Command cacheserver runs the local artifact cache and fixture-command
// build service. It uses only local disk — no cloud endpoints.
package main

import (
	"flag"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"localcache/internal/builder"
	"localcache/internal/cas"
	"localcache/internal/server"
)

func main() {
	var (
		addr       = flag.String("addr", "127.0.0.1:8080", "listen address")
		cacheDir   = flag.String("cache-dir", "./cache-data", "cache root (objects, tmp, ac); must be separate from -work-dir")
		workDir    = flag.String("work-dir", "", "root for per-build working directories (default: a fresh dir under the OS temp dir); must differ from -cache-dir")
		maxObjSize = flag.Int64("max-object-size", 64<<20, "maximum object size in bytes")
		maxBuildTo = flag.Duration("max-build-timeout", 30*time.Second, "upper bound for a single fixture command")
	)
	flag.Parse()

	absCache, err := filepath.Abs(*cacheDir)
	if err != nil {
		log.Fatalf("resolving cache dir: %v", err)
	}
	work := *workDir
	if work == "" {
		work, err = os.MkdirTemp("", "localcache-work-*")
		if err != nil {
			log.Fatalf("creating work dir: %v", err)
		}
	}
	absWork, err := filepath.Abs(work)
	if err != nil {
		log.Fatalf("resolving work dir: %v", err)
	}
	if absCache == absWork {
		log.Fatal("cache dir and work dir must be different directories")
	}

	store, err := cas.Open(absCache, *maxObjSize)
	if err != nil {
		log.Fatalf("opening cache: %v", err)
	}
	exec, err := builder.NewExecutor(store, absWork, *maxBuildTo)
	if err != nil {
		log.Fatalf("creating executor: %v", err)
	}
	srv, err := server.New(store, exec, absCache)
	if err != nil {
		log.Fatalf("creating server: %v", err)
	}

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Printf("cacheserver: listening on %s", *addr)
	log.Printf("cacheserver: cache dir %s (max object %d bytes)", absCache, *maxObjSize)
	log.Printf("cacheserver: work dir  %s (max build timeout %s)", absWork, *maxBuildTo)
	if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("serving: %v", err)
	}
}
