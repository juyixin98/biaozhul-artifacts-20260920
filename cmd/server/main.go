// Command server runs the local multi-architecture OCI platform-selection API.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.example.com/ocimultipick/internal/api"
	"github.example.com/ocimultipick/internal/blobstore"
	"github.example.com/ocimultipick/internal/oci"
	"github.example.com/ocimultipick/internal/store"
)

func main() {
	var (
		addr     = flag.String("addr", ":8080", "HTTP listen address")
		registry = flag.String("registry", "./registry", "directory holding OCI image-layout repositories")
		dbPath   = flag.String("db", "./data/ocimultipick.db", "SQLite database path")
	)
	flag.Parse()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	blobs := blobstore.New(*registry)

	if dir := filepath.Dir(*dbPath); dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			log.Fatalf("create database directory: %v", err)
		}
	}

	db, err := store.Open(ctx, *dbPath)
	if err != nil {
		log.Fatalf("open database: %v", err)
	}
	defer db.Close()

	if err := seedTags(ctx, blobs, db); err != nil {
		log.Fatalf("seed tags: %v", err)
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.NewServer(blobs, db).Router(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("OCI multi-arch selection API listening on %s (registry=%q db=%q)", *addr, *registry, *dbPath)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutdown signal received")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
	}
}

// seedTags imports tag pointers from each repository's index.json (the
// org.opencontainers.image.ref.name annotation). Existing tags are left
// untouched: a tag moved via the API is mutable metadata and must survive a
// restart, whereas index.json is only the initial seeding source.
func seedTags(ctx context.Context, blobs *blobstore.Store, db *store.Store) error {
	repos, err := blobs.ListRepos()
	if err != nil {
		return fmt.Errorf("listing repositories: %w", err)
	}
	seeded := 0
	for _, repo := range repos {
		idx, err := blobs.LoadIndex(repo)
		if err != nil {
			return fmt.Errorf("loading index of %s: %w", repo, err)
		}
		for _, desc := range idx.Manifests {
			tag := desc.Annotations[oci.AnnotationRefName]
			if tag == "" {
				continue
			}
			if _, err := db.GetTag(ctx, repo, tag); err == nil {
				continue // never overwrite a tag that may have been moved
			}
			if err := db.UpsertTag(ctx, store.Tag{
				Repository: repo,
				Tag:        tag,
				Digest:     desc.Digest,
				MediaType:  desc.MediaType,
			}); err != nil {
				return fmt.Errorf("seeding %s:%s: %w", repo, tag, err)
			}
			seeded++
		}
	}
	log.Printf("seeded %d tag(s) from OCI layouts", seeded)
	return nil
}
