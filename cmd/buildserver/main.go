// Command buildserver runs the local build engineering service.
//
// It executes ONLY the fixture commands declared in the manifest. The cache
// URL, work directory and artifact directory are all configured by the
// operator; job scratch space is never created inside the cache root.
package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"modelcache/buildsvc"
	"modelcache/cacheapi"
	"modelcache/client"
)

func main() {
	addr := flag.String("addr", envOr("BUILD_ADDR", ":8081"), "listen address")
	cacheURL := flag.String("cache-url", envOr("CACHE_URL", "http://127.0.0.1:8080"), "remote cache base URL")
	manifestPath := flag.String("fixtures", envOr("FIXTURES", "fixtures/manifest.json"), "fixture allowlist manifest")
	workDir := flag.String("work-dir", envOr("WORK_DIR", "./work"), "job scratch root (must differ from cache dir)")
	artifactDir := flag.String("artifact-dir", envOr("ARTIFACT_DIR", "./artifacts"), "local copy of produced artifacts (optional)")
	defaultTimeout := flag.Duration("default-timeout", 5*time.Minute, "default job timeout")
	maxTimeout := flag.Duration("max-timeout", 30*time.Minute, "maximum job timeout")
	flag.Parse()

	logger := log.New(os.Stderr, "buildserver ", log.LstdFlags|log.Lmicroseconds)

	manifest, err := buildsvc.LoadManifest(*manifestPath)
	if err != nil {
		logger.Fatalf("load fixtures: %v", err)
	}
	logger.Printf("loaded %d fixture(s) from %s", len(manifest.Fixtures), *manifestPath)

	cache := client.New(client.Options{BaseURL: *cacheURL, UserAgent: "buildserver/1.0"})
	svc, err := buildsvc.New(buildsvc.Options{
		WorkDir:      *workDir,
		ArtifactDir:  *artifactDir,
		Manifest:     manifest,
		Cache:        cache,
		DefaultLimit: *defaultTimeout,
		MaxLimit:     *maxTimeout,
		Logger:       logger,
	})
	if err != nil {
		logger.Fatalf("build service: %v", err)
	}
	defer svc.Close()

	hsrv := buildsvc.NewHTTPServer(svc, logger)
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if err := cacheapi.ListenAndServe(ctx, *addr, hsrv.Handler(), logger); err != nil {
		logger.Fatalf("server error: %v", err)
	}
	logger.Printf("shutdown complete")
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
