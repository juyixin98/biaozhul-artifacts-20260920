// Command server runs the local domain lifecycle engine: it migrates the
// database, seeds a bootstrap admin token (printed once at startup), serves the
// HTTP API, and runs the background lifecycle worker.
//
// No real registrar, DNS or payment provider is contacted: transfers,
// expirations and payments are all simulated locally.
package main

import (
	"context"
	"encoding/hex"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/labstack/echo/v4"
	"github.com/labstack/echo/v4/middleware"

	"domainengine/internal/accounts"
	clk "domainengine/internal/clock"
	"domainengine/internal/codec"
	"domainengine/internal/config"
	"domainengine/internal/domains"
	httpapi "domainengine/internal/httpapi"
	"domainengine/internal/ledger"
	"domainengine/internal/prices"
	"domainengine/internal/store"
	"domainengine/internal/transfers"
	"domainengine/internal/worker"
)

func main() {
	logger := log.New(os.Stdout, "engine: ", log.LstdFlags|log.Lmsgprefix)
	cfg := config.Load()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	db, err := store.EnsureOpen(ctx, cfg.DatabaseURL)
	if err != nil {
		logger.Fatalf("database: %v", err)
	}
	defer db.Close()

	if err := store.RunMigrations(ctx, db); err != nil {
		logger.Fatalf("migrations: %v", err)
	}

	key, err := hex.DecodeString(cfg.CodecKeyHex)
	if err != nil {
		logger.Fatalf("CODEC_KEY_HEX must be 64 hex chars: %v", err)
	}
	code, err := codec.New(key)
	if err != nil {
		logger.Fatalf("codec: %v", err)
	}

	// Services.
	accSvc := accounts.New(db)
	ledgerSvc := ledger.New(db)
	priceSvc := prices.New(db)
	timeline := domains.Timeline{
		ExpiredGrace:  cfg.ExpiredGrace,
		RedeemPeriod:  cfg.RedeemPeriod,
		PendingDelete: cfg.PendingDelete,
	}
	domainSvc := domains.New(db, ledgerSvc, priceSvc, code, timeline)
	xferSvc := transfers.New(db, domainSvc, priceSvc, ledgerSvc, transfers.Config{
		ApprovalWindow: cfg.ApprovalWindow,
		TransferWait:   5 * 24 * time.Hour, // simulated 5-day registry wait
		Timeline:       timeline,
	})

	// Bootstrap admin (token printed once).
	if token, created, err := accSvc.EnsureAdminPrincipal(ctx); err != nil {
		logger.Fatalf("admin seed: %v", err)
	} else if created {
		logger.Printf("bootstrap admin token (shown once): %s", token)
	}

	// HTTP.
	e := echo.New()
	e.HideBanner = true
	// Logger records only method/path/status/latency — never headers, bodies
	// or query strings, which may carry bearer tokens / auth codes.
	e.Use(middleware.LoggerWithConfig(middleware.LoggerConfig{
		Format: `${time_rfc3339} method=${method} uri=${uri} status=${status} latency=${latency_human}` + "\n",
	}))
	e.Use(middleware.Recover())

	api := httpapi.New(db, accSvc, domainSvc, xferSvc, priceSvc, ledgerSvc, clk.SystemClock{})
	api.Handler(e)

	srv := &http.Server{Addr: cfg.HTTPAddr, Handler: e}
	go func() {
		logger.Printf("listening on %s", cfg.HTTPAddr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Fatalf("http: %v", err)
		}
	}()

	// Background worker.
	w := worker.New(store.NewSessionLocks(db), domainSvc, xferSvc,
		clk.SystemClock{}, cfg.SweepInterval, cfg.PollInterval, logger)
	go w.Run(ctx)

	<-ctx.Done()
	logger.Printf("shutting down")
	shCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(shCtx)
}
