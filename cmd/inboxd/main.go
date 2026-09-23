// Command inboxd runs the cross-chain message inbox HTTP service.
//
// Configuration via environment:
//
//	INBOX_DB        PostgreSQL DSN (default: postgres://inbox:inbox@localhost:55432/inbox?sslmode=disable)
//	INBOX_ADDR      listen address (default: :8090)
//	INBOX_INTERVAL  background processing interval, e.g. 250ms (default: 200ms)
//	INBOX_CRASH     test-only crash point: "before_deliver" or "before_commit",
//	                optionally followed by "@<nonce>" to crash only once for that
//	                nonce on the first channel that reaches it. The process
//	                exits with code 99. Never set this in production.
package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"inbox/internal/api"
	"inbox/internal/core"
	"inbox/internal/fixtures"
	"inbox/internal/store"
)

func main() {
	dsn := envOr("INBOX_DB", "postgres://inbox:inbox@localhost:55432/inbox?sslmode=disable")
	addr := envOr("INBOX_ADDR", ":8090")
	interval, err := time.ParseDuration(envOr("INBOX_INTERVAL", "200ms"))
	if err != nil {
		log.Fatalf("invalid INBOX_INTERVAL: %v", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.New(ctx, dsn)
	if err != nil {
		log.Fatalf("database: %v", err)
	}
	defer st.Close()
	if err := st.Migrate(ctx); err != nil {
		log.Fatalf("migrate: %v", err)
	}

	ex := core.NewExecutor(st)

	// Register the two deterministic simulated chains.
	var seed []core.ChainConfig
	for _, id := range fixtures.ChainIDs() {
		cf := fixtures.Chains[id]
		seed = append(seed, core.ChainConfig{
			ID: id, ValidatorPub: cf.Validator.Pub, Confirmations: cf.Confirmations,
		})
	}
	if err := ex.SeedChains(ctx, seed); err != nil {
		log.Fatalf("seed chains: %v", err)
	}

	installCrashHook(ex)

	srv := &http.Server{
		Addr:              addr,
		Handler:           api.NewRouter(ex),
		ReadHeaderTimeout: 10 * time.Second,
	}

	// Background processing loop: wakes on notifications or interval.
	// Disabled entirely with INBOX_WORKER=off (used by tests that drive
	// processing explicitly via POST /v1/process).
	workerDone := make(chan struct{})
	if os.Getenv("INBOX_WORKER") != "off" {
		go func() {
			defer close(workerDone)
			ticker := time.NewTicker(interval)
			defer ticker.Stop()
			for {
				select {
				case <-ctx.Done():
					return
				case <-ticker.C:
				case <-ex.Wake():
				}
				n, err := ex.ProcessAll(context.Background())
				if err != nil {
					log.Printf("processing tick error: %v", err)
					continue
				}
				if n > 0 {
					log.Printf("delivered %d message(s)", n)
				}
			}
		}()
	} else {
		close(workerDone)
		log.Printf("background worker disabled (INBOX_WORKER=off); drive POST /v1/process manually")
	}

	go func() {
		log.Printf("inbox listening on %s (db %s)", addr, redactDSN(dsn))
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutting down")
	shCtx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	if err := srv.Shutdown(shCtx); err != nil {
		log.Printf("http shutdown: %v", err)
	}
	<-workerDone
}

// installCrashHook wires INBOX_CRASH into a process-exit crash point.
func installCrashHook(ex *core.Executor) {
	spec := strings.TrimSpace(os.Getenv("INBOX_CRASH"))
	if spec == "" {
		return
	}
	point := spec
	var targetNonce uint64
	if i := strings.Index(spec, "@"); i >= 0 {
		point = spec[:i]
		n, err := strconv.ParseUint(spec[i+1:], 10, 64)
		if err != nil {
			log.Fatalf("invalid INBOX_CRASH spec: %v", err)
		}
		targetNonce = n
	}
	if point != "before_deliver" && point != "before_commit" {
		log.Fatalf("INBOX_CRASH point must be before_deliver or before_commit")
	}
	fired := false
	ex.SetCrashHook(func(p, _, _ string, nonce uint64) {
		if p != point || fired {
			return
		}
		// Without a nonce filter the hook fires on the first delivery
		// attempt; with a filter it fires only for that nonce.
		if targetNonce != 0 && nonce != targetNonce {
			return
		}
		fired = true
		log.Printf("CRASH POINT %s REACHED (nonce=%d) — exiting 99 to simulate hard crash", p, nonce)
		os.Exit(99)
	})
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func redactDSN(dsn string) string {
	at := strings.Index(dsn, "@")
	colon := strings.Index(dsn, ":")
	if at > colon && colon > 8 {
		return dsn[:colon+1] + "***" + dsn[at:]
	}
	return dsn
}
