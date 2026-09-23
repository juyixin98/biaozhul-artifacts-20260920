// Command snapd runs the account-state snapshot/pruning service.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/example/snapshotprune/internal/engine"
	"github.com/example/snapshotprune/internal/server"
	"github.com/example/snapshotprune/internal/types"
)

func main() {
	var (
		addr      = flag.String("addr", "127.0.0.1:8080", "HTTP listen address")
		dataDir   = flag.String("data", "./data", "data directory (Pebble + snapshots + node key)")
		genesis   = flag.String("genesis", "", "genesis JSON file (required on first boot)")
		chainID   = flag.String("chain-id", "demo-1", "chain id (must match genesis)")
		leaseTTL  = flag.Duration("lease-ttl", 5*time.Second, "reader lease TTL")
		snapEvery = flag.Duration("snapshot-every", 0, "periodic snapshot+prune interval (0 = manual only)")
		verbose   = flag.Bool("v", false, "verbose logging")
	)
	flag.Parse()

	level := slog.LevelInfo
	if *verbose {
		level = slog.LevelDebug
	}
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: level}))

	cfg := engine.Config{
		DataDir:        *dataDir,
		ChainID:        *chainID,
		LeaseTTL:       *leaseTTL,
		SnapshotPeriod: *snapEvery,
	}
	if *genesis != "" {
		g, err := loadGenesis(*genesis, *chainID)
		if err != nil {
			log.Error("invalid genesis", "err", err)
			os.Exit(2)
		}
		cfg.Genesis = g
	}

	eng, err := engine.Open(cfg)
	if err != nil {
		log.Error("open engine", "err", err)
		os.Exit(1)
	}
	eng.StartPeriodic()
	defer eng.Close()

	srv := server.New(eng, *addr, log)
	errCh := make(chan error, 1)
	go func() { errCh <- srv.Start() }()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	select {
	case err := <-errCh:
		if err != nil {
			log.Error("server stopped", "err", err)
			os.Exit(1)
		}
	case sig := <-sigCh:
		log.Info("shutting down", "signal", sig.String())
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := srv.Shutdown(ctx); err != nil {
			log.Error("graceful shutdown", "err", err)
		}
	}
}

func loadGenesis(path, chainID string) (*types.Genesis, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var g types.Genesis
	if err := json.Unmarshal(b, &g); err != nil {
		return nil, err
	}
	if g.ChainID == "" {
		g.ChainID = chainID
	}
	if g.ChainID != chainID {
		return nil, os.ErrInvalid
	}
	if len(g.Allocations) == 0 {
		return nil, os.ErrInvalid
	}
	return &g, nil
}
