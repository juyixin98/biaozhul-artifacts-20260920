// Command attestation-server is the build-attestation verification service.
package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"build-attestation/internal/attestation"
	"build-attestation/internal/httpapi"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	policyPath := flag.String("policy", "testdata/policy.json", "path to trust policy JSON")
	window := flag.Duration("window", 5*time.Minute, "attestation freshness window (±)")
	nowOverride := flag.String("now", "", "TEST ONLY: pin the verifier clock to this RFC3339 timestamp")
	flag.Parse()

	policy, err := attestation.LoadPolicy(*policyPath)
	if err != nil {
		log.Fatalf("load policy: %v", err)
	}
	cfg := attestation.Config{
		Policy:          policy,
		FreshnessWindow: *window,
	}
	if *nowOverride != "" {
		t, perr := time.Parse(time.RFC3339, *nowOverride)
		if perr != nil {
			log.Fatalf("parse -now: %v", perr)
		}
		log.Printf("WARNING: clock pinned to %s (test mode; do not use in production)", t.Format(time.RFC3339))
		cfg.Now = func() time.Time { return t }
	}
	verifier, err := attestation.NewVerifier(cfg)
	if err != nil {
		log.Fatalf("init verifier: %v", err)
	}

	srv := httpapi.New(verifier)
	log.Printf("build-attestation verifier listening on %s (policy=%s, window=%s)", *addr, *policyPath, *window)
	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Fatal(httpSrv.ListenAndServe())
}
