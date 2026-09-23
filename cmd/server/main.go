// Command mirror-admission-server is the offline local image admission
// backend. It loads a frozen, hash-verified OPA policy and an allowlist,
// binds two Ed25519 trust roots, and serves the JSON API with chi. It never
// contacts a Kubernetes cluster or any registry.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"mirror-admission/internal/api"
	"mirror-admission/internal/policy"
	"mirror-admission/internal/service"
	"mirror-admission/internal/store"
	"mirror-admission/internal/verifier"

	edsig "mirror-admission/internal/crypto/sig"
)

type config struct {
	addr          string
	policyPath    string
	freezeLock    string
	allowlistPath string
	verifierPub   string
	exemptPub     string
	reportsPath   string
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func loadConfig() config {
	c := config{
		addr:          envOr("MIRRORAD_LISTEN", ":8080"),
		policyPath:    envOr("MIRRORAD_POLICY", "policy/admission.rego"),
		freezeLock:    envOr("MIRRORAD_POLICY_LOCK", "policy/policy_freeze.lock.json"),
		allowlistPath: envOr("MIRRORAD_ALLOWLIST", "config/allowlist.json"),
		verifierPub:   envOr("MIRRORAD_VERIFIER_PUB", "keys/tester_public.pem"),
		exemptPub:     envOr("MIRRORAD_EXEMPT_PUB", "keys/exempt_authority_public.pem"),
		reportsPath:   envOr("MIRRORAD_REPORTS", "data/reports.jsonl"),
	}
	flag.StringVar(&c.addr, "listen", c.addr, "listen address")
	flag.StringVar(&c.policyPath, "policy", c.policyPath, "rego policy path")
	flag.StringVar(&c.freezeLock, "policy-lock", c.freezeLock, "frozen policy hash lock")
	flag.StringVar(&c.allowlistPath, "allowlist", c.allowlistPath, "base image allowlist")
	flag.StringVar(&c.verifierPub, "verifier-pub", c.verifierPub, "tester public key PEM")
	flag.StringVar(&c.exemptPub, "exempt-pub", c.exemptPub, "exemption authority public key PEM")
	flag.StringVar(&c.reportsPath, "reports", c.reportsPath, "append-only reports JSONL path")
	flag.Parse()
	return c
}

func main() {
	cfg := loadConfig()
	if err := run(cfg); err != nil {
		log.Fatalf("mirror-admission: %v", err)
	}
}

func run(cfg config) error {
	// 1. Frozen policy: hash must match the lock file or refuse to start.
	polBytes, polHash, err := policy.LoadAndVerify(cfg.policyPath, cfg.freezeLock)
	if err != nil {
		return err
	}
	engine, err := policy.NewEngine(polBytes, polHash)
	if err != nil {
		return err
	}
	log.Printf("loaded frozen policy version %s (sha256:%s)", engine.Version(), engine.SHA256())

	// 2. Allowlist.
	allowed, err := loadAllowlist(cfg.allowlistPath)
	if err != nil {
		return err
	}
	log.Printf("loaded allowlist %s (%d base images)", allowed.Version, len(allowed.AllowedBaseImages))

	// 3. Trust roots (two DIFFERENT keys).
	vPub, err := edsig.ReadPublicKeyPEM(cfg.verifierPub)
	if err != nil {
		return fmt.Errorf("load verifier public key: %w", err)
	}
	ePub, err := edsig.ReadPublicKeyPEM(cfg.exemptPub)
	if err != nil {
		return fmt.Errorf("load exemption public key: %w", err)
	}
	vID, _ := edsig.KeyID(vPub)
	eID, _ := edsig.KeyID(ePub)
	if vID == eID {
		return fmt.Errorf("verifier and exemption trust roots are the SAME key %s; separation of duties forbids this", vID)
	}
	log.Printf("trust roots: tester=%s exemption_authority=%s", vID, eID)

	// 4. Append-only report store.
	if err := os.MkdirAll(dirOf(cfg.reportsPath), 0o755); err != nil {
		return err
	}
	repStore, err := store.NewJSONL(cfg.reportsPath)
	if err != nil {
		return err
	}

	svc := service.New(engine, verifier.Keys{Verifier: vPub, Exemption: ePub}, allowed, repStore)

	// Test-only fixed clock: MIRRORAD_NOW pins evaluation time so boundary
	// (expiry == now) behaviour is reproducible. Never set in production.
	if fixed := os.Getenv("MIRRORAD_NOW"); fixed != "" {
		t, err := time.Parse(time.RFC3339Nano, fixed)
		if err != nil {
			t, err = time.Parse(time.RFC3339, fixed)
		}
		if err != nil {
			return fmt.Errorf("invalid MIRRORAD_NOW %q: %w", fixed, err)
		}
		log.Printf("WARNING: using TEST-ONLY fixed clock %s", t.UTC().Format(time.RFC3339Nano))
		svc = svc.WithClock(fixedClock{t: t.UTC()})
	}

	handler := api.NewServer(svc)

	srv := &http.Server{
		Addr:              cfg.addr,
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	go func() {
		log.Printf("listening on %s (offline; no cluster connection)", cfg.addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server: %v", err)
		}
	}()
	<-stop
	log.Printf("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return srv.Shutdown(ctx)
}

func dirOf(p string) string {
	for i := len(p) - 1; i >= 0; i-- {
		if p[i] == '/' {
			return p[:i]
		}
	}
	return "."
}

type fixedClock struct{ t time.Time }

func (c fixedClock) Now() time.Time { return c.t }

func loadAllowlist(path string) (policy.Allowlist, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return policy.Allowlist{}, fmt.Errorf("read allowlist: %w", err)
	}
	var a policy.Allowlist
	if err := json.Unmarshal(b, &a); err != nil {
		return policy.Allowlist{}, fmt.Errorf("parse allowlist: %w", err)
	}
	if a.Version == "" {
		return policy.Allowlist{}, fmt.Errorf("allowlist %s has no version", path)
	}
	if len(a.AllowedBaseImages) == 0 {
		return policy.Allowlist{}, fmt.Errorf("allowlist %s has no allowed_base_images", path)
	}
	seen := map[string]bool{}
	for _, d := range a.AllowedBaseImages {
		if len(d) != len("sha256:")+64 || d[:7] != "sha256:" {
			return policy.Allowlist{}, fmt.Errorf("allowlist entry %q is not sha256:<64hex>", d)
		}
		if seen[d] {
			return policy.Allowlist{}, fmt.Errorf("allowlist contains duplicate %s", d)
		}
		seen[d] = true
	}
	return a, nil
}
