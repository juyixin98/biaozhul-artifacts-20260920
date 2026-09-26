// Command idemresp runs the idempotent-order demo entirely on localhost:
//
//   - the application HTTP API (default :18080)
//   - the in-process FAKE payment gateway (default :18081)
//
// Nothing here talks to any real or production system. Data is kept in a
// local write-ahead log under --data-dir and survives restarts.
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

	"idemresp/internal/client"
	"idemresp/internal/clock"
	"idemresp/internal/gateway"
	"idemresp/internal/idem"
	"idemresp/internal/server"
	"idemresp/internal/txn"
)

func main() {
	appAddr := flag.String("addr", ":18080", "application HTTP listen address")
	gwAddr := flag.String("gateway-addr", ":18081", "in-process fake gateway HTTP listen address")
	gwURL := flag.String("gateway-url", "", "use an external fake gateway at this base URL instead of starting one in-process")
	dataDir := flag.String("data-dir", "./data", "directory for the local WAL")
	lease := flag.Duration("lease", 30*time.Second, "processing-claim lease after a crash")
	gwTimeout := flag.Duration("gateway-timeout", 2*time.Second, "per-attempt gateway HTTP timeout")
	allowCrash := flag.Bool("allow-crash", false, "honor X-Crash injection headers (test only)")
	flag.Parse()

	logger := log.New(os.Stdout, "app ", log.LstdFlags|log.Lmicroseconds)
	gwLogger := log.New(os.Stdout, "gw  ", log.LstdFlags|log.Lmicroseconds)

	db, err := txn.Open(*dataDir, clock.Real{})
	if err != nil {
		logger.Fatalf("open store: %v", err)
	}
	defer func() { _ = db.Close() }()

	gw := gateway.NewServer(nil)
	gwBase := *gwURL
	if gwBase == "" {
		gwBase = "http://" + *gwAddr
	}
	gwClient := client.New(gwBase, *gwTimeout)
	gwClient.Trace = func(a client.Attempt) {
		gwLogger.Printf("charge attempt %d -> %s status=%d err=%q", a.N, a.Outcome, a.HTTPStatus, a.Error)
	}

	svc := idem.New(idem.Config{
		DB:               db,
		GW:               gwClient,
		Clk:              clock.Real{},
		Lease:            *lease,
		AmbiguousRetries: 1,
		Logf:             logger.Printf,
		Crash:            crashHook(*allowCrash),
	})

	var gwSrv *http.Server
	if *gwURL == "" {
		gwSrv = &http.Server{
			Addr:              *gwAddr,
			Handler:           gw.Handler(),
			ReadHeaderTimeout: 5 * time.Second,
		}
	}
	appSrv := &http.Server{
		Addr:              *appAddr,
		Handler:           server.New(server.Deps{Service: svc, DB: db, Logger: logger, AllowCrash: *allowCrash}),
		ReadHeaderTimeout: 5 * time.Second,
	}

	if gwSrv != nil {
		go func() {
			gwLogger.Printf("fake payment gateway listening on %s", *gwAddr)
			if err := gwSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
				gwLogger.Fatalf("gateway server: %v", err)
			}
		}()
	} else {
		gwLogger.Printf("using external fake gateway at %s", gwBase)
	}
	go func() {
		logger.Printf("idempotent orders API listening on %s (data dir %s)", *appAddr, *dataDir)
		if err := appSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Fatalf("app server: %v", err)
		}
	}()

	sigc := make(chan os.Signal, 1)
	signal.Notify(sigc, syscall.SIGINT, syscall.SIGTERM)
	<-sigc
	logger.Printf("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = appSrv.Shutdown(shutdownCtx)
	if gwSrv != nil {
		_ = gwSrv.Shutdown(shutdownCtx)
	}
}

// crashHook returns the real process-crash function only when explicitly
// enabled; otherwise crash headers are ignored.
func crashHook(allowed bool) func(stage string) {
	if !allowed {
		return nil
	}
	return server.CrashFn
}
