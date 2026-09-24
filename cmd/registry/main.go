// Command registry runs the local content-addressable image registry with
// safe, reference-aware garbage collection.
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

	"layerregistry/internal/gc"
	"layerregistry/internal/registry"
	"layerregistry/internal/storage"
	"layerregistry/internal/store"
)

func main() {
	var (
		addr         = flag.String("addr", envOr("REGISTRY_ADDR", ":8080"), "HTTP listen address")
		databaseURL  = flag.String("db", envOr("DATABASE_URL", "postgres://registry:registry_pw@127.0.0.1:5432/registry?sslmode=disable"), "PostgreSQL connection URL")
		dataDir      = flag.String("data", envOr("REGISTRY_DATA", "./data"), "on-disk content root")
		leaseTTL     = flag.Duration("lease-ttl", 60*time.Second, "read lease time-to-live")
		blobGrace    = flag.Duration("blob-grace", 30*time.Second, "new-blob GC grace period")
		orphanMaxAge = flag.Duration("orphan-max-age", 2*time.Hour, "upload session age after which its temp file is an orphan")
		faults       = flag.Bool("faults", envOr("REGISTRY_FAULTS", "") == "1", "enable test-only fault injection endpoints")
		recoverOnly  = flag.Bool("recover", false, "run crash recovery on startup, then continue serving")
	)
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.New(ctx, *databaseURL)
	if err != nil {
		log.Fatalf("connect/migrate database: %v", err)
	}
	defer st.Close()

	fs, err := storage.New(*dataDir)
	if err != nil {
		log.Fatalf("init storage at %s: %v", *dataDir, err)
	}

	col := gc.New(st, fs, gc.Config{BlobGrace: *blobGrace})
	if *recoverOnly || os.Getenv("REGISTRY_RECOVER_ON_START") == "1" {
		lines, err := col.Recover(ctx)
		if err != nil {
			log.Fatalf("crash recovery failed: %v", err)
		}
		for _, l := range lines {
			log.Printf("recover: %s", l)
		}
		strays, err := col.ReconcileBlobFiles(ctx)
		if err != nil {
			log.Fatalf("reconcile failed: %v", err)
		}
		for _, d := range strays {
			log.Printf("recover: stray blob file without DB row: %s", d)
		}
	}

	srv := registry.NewServer(st, fs, registry.Config{
		LeaseTTL:      *leaseTTL,
		BlobGrace:     *blobGrace,
		OrphanMaxAge:  *orphanMaxAge,
		FaultsEnabled: *faults,
	})

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 15 * time.Second,
	}

	go func() {
		log.Printf("registry listening on %s (data=%s, faults=%v)", *addr, *dataDir, *faults)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutting down")
	shutCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(shutCtx); err != nil {
		log.Printf("graceful shutdown: %v", err)
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
