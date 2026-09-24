// Command atomicpromo 启动“产物晋级原子性”后端服务。
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"atomicpromo/internal/blob"
	"atomicpromo/internal/httpapi"
	"atomicpromo/internal/promotion"
	"atomicpromo/internal/receipt"
	"atomicpromo/internal/store"
)

func main() {
	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	dsn := store.DSNFromEnv()
	db, err := store.Open(ctx, dsn)
	if err != nil {
		logger.Error("connect database", "err", err)
		os.Exit(1)
	}
	defer db.Close()

	migDir := envOr("MIGRATIONS_DIR", "migrations")
	if err := db.Migrate(ctx, migDir); err != nil {
		logger.Error("migrate", "err", err)
		os.Exit(1)
	}
	logger.Info("migrations applied", "dir", migDir)

	blobRoot := envOr("BLOB_STORE_DIR", filepath.Join(os.TempDir(), "atomicpromo-blobs"))
	blobs, err := blob.New(blobRoot)
	if err != nil {
		logger.Error("blob store", "err", err)
		os.Exit(1)
	}
	signer, err := receipt.LoadOrCreateSigner(envOr("SIGNING_KEY_DIR", filepath.Join(blobRoot, "keys")))
	if err != nil {
		logger.Error("receipt signer", "err", err)
		os.Exit(1)
	}
	logger.Info("receipt signing key ready", "key_id", signer.KeyID())

	svc := promotion.New(db, blobs, signer, logger)

	// 启动恢复：先收尾上次中断的尝试（旧指针一直在线，新进程启动期间服务不受影响）
	rep, err := svc.RecoverOnStartup(ctx)
	if err != nil {
		logger.Error("startup recovery", "err", err)
		os.Exit(1)
	}
	logger.Info("startup recovery",
		"recovered_committed", rep.RecoveredCommitted,
		"recovered_aborted", rep.RecoveredAborted,
		"awaiting_approval", rep.PendingApprovals)

	srv := &http.Server{
		Addr:              envOr("HTTP_ADDR", ":8080"),
		Handler:           httpapi.New(svc, logger).Router(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		logger.Info("listening", "addr", srv.Addr, "blob_root", blobRoot,
			"fault_injection", os.Getenv("ALLOW_FAULT_INJECTION"))
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("http server", "err", err)
			os.Exit(1)
		}
	}()

	<-ctx.Done()
	logger.Info("shutting down")
	shutdownCtx, c := context.WithTimeout(context.Background(), 5*time.Second)
	defer c()
	_ = srv.Shutdown(shutdownCtx)
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
