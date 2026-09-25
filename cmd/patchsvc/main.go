// Command patchsvc runs the local patch-context application service.
//
// It is a pure backend: a JSON/HTTP API on a local address, no cloud
// connectivity, with all staging state confined to -cache-dir.
package main

import (
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"patchsvc/internal/server"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8787", "listen address (host:port)")
	cacheDir := flag.String("cache-dir", defaultCacheDir(), "directory for staging/backup state; must be separate from every work directory")
	flag.Parse()

	srv, err := server.New(*cacheDir)
	if err != nil {
		log.Fatalf("patchsvc: %v", err)
	}

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Printf("patchsvc: listening on http://%s (cache dir: %s)", *addr, srv.CacheDir)
	if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("patchsvc: %v", err)
	}
}

func defaultCacheDir() string {
	if dir, err := os.UserCacheDir(); err == nil && dir != "" {
		return filepath.Join(dir, "patchsvc")
	}
	return filepath.Join(os.TempDir(), fmt.Sprintf("patchsvc-cache-%d", os.Getuid()))
}
