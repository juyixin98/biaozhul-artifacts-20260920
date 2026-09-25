// depscan is a local build-engineering service that scans C-family source
// trees for #include dependencies and answers incremental "what is affected"
// queries over a JSON API. It is purely local: no cloud, no subprocesses.
package main

import (
	"flag"
	"log"
	"net/http"
	"os"
	"path/filepath"

	"depscan/internal/api"
	"depscan/internal/service"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address (loopback by default)")
	cacheDir := flag.String("cache-dir", defaultCacheDir(), "cache directory (must be separate from scanned source roots)")
	flag.Parse()

	svc, err := service.NewService(*cacheDir)
	if err != nil {
		log.Fatalf("init service: %v", err)
	}
	srv := api.NewServer(svc)
	log.Printf("depscan listening on http://%s (cache: %s)", *addr, *cacheDir)
	log.Fatal(http.ListenAndServe(*addr, srv.Handler()))
}

func defaultCacheDir() string {
	if dir, err := os.UserCacheDir(); err == nil {
		return filepath.Join(dir, "depscan")
	}
	return filepath.Join(os.TempDir(), "depscan-cache")
}
