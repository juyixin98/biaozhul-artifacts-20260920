// Command licensejudge is a local backend service that evaluates SPDX
// license-expression subsets against a custom policy. It exposes JSON
// endpoints and runs only whitelisted local test-fixture commands.
//
// It makes configuration decisions only and never gives legal conclusions.
package main

import (
	"flag"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"licensejudge/internal/api"
	"licensejudge/internal/policy"
	"licensejudge/internal/runner"
)

func main() {
	var (
		addr       = flag.String("addr", "127.0.0.1:8080", "listen address (local only by default)")
		policyPath = flag.String("policy", "configs/policy.json", "path to policy JSON")
		fixPath    = flag.String("fixtures", "configs/fixtures.json", "path to fixture-command manifest")
		workDir    = flag.String("work-dir", ".local/work", "scratch work directory for fixture commands")
		cacheDir   = flag.String("cache-dir", ".local/cache", "persistent fixture-result cache (separate from work dir)")
	)
	flag.Parse()

	pol, err := policy.Load(*policyPath)
	if err != nil {
		log.Fatalf("load policy: %v", err)
	}
	manifest, err := runner.LoadManifest(*fixPath)
	if err != nil {
		log.Fatalf("load fixtures: %v", err)
	}

	absBase, err := os.Getwd()
	if err != nil {
		log.Fatal(err)
	}
	absWork, err := filepath.Abs(*workDir)
	if err != nil {
		log.Fatal(err)
	}
	absCache, err := filepath.Abs(*cacheDir)
	if err != nil {
		log.Fatal(err)
	}
	rn, err := runner.NewRunner(manifest, absBase, absWork, absCache, 30*time.Second)
	if err != nil {
		log.Fatalf("init runner: %v", err)
	}
	log.Printf("work dir: %s", rn.WorkDir())
	log.Printf("cache dir: %s", rn.CacheDir())

	srv := &api.Server{Policy: pol, Runner: rn}
	httpServer := &http.Server{
		Addr:              *addr,
		Handler:           srv.NewMux(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Printf("licensejudge listening on http://%s", *addr)
	log.Fatal(httpServer.ListenAndServe())
}
