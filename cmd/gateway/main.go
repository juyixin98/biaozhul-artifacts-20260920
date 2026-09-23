// Command gateway runs the protocol-version compatibility gateway.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"syscall"
	"time"

	gatewayv1 "github.com/p079/telegw/gen/gateway/v1"
	"github.com/p079/telegw/internal/registry"
	"github.com/p079/telegw/internal/server"
	"github.com/p079/telegw/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/reflection"
)

func main() {
	var (
		addr           = flag.String("addr", envOr("GATEWAY_ADDR", ":50051"), "gRPC listen address")
		mappingsPath   = flag.String("mappings", envOr("GATEWAY_MAPPINGS", "mappings.json"), "path to mappings.json")
		initialMapping = flag.String("mapping", envOr("GATEWAY_MAPPING", "strict-v1"), "initial active mapping version")
		dsn            = flag.String("dsn", os.Getenv("GATEWAY_DATABASE_DSN"), "PostgreSQL DSN (empty disables audit persistence)")
		bufSize        = flag.Int("buffer", server.DefaultBufferSize, "per-stream in-flight buffer (backpressure bound)")
	)
	flag.Parse()

	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	reg, err := registry.Load(*mappingsPath, *initialMapping)
	if err != nil {
		log.Error("load mappings", "error", err)
		os.Exit(1)
	}
	log.Info("mappings loaded", "active", reg.Active().Version, "available", reg.Versions())

	var st *store.Store
	if *dsn != "" {
		st, err = store.Connect(ctx, *dsn)
		if err != nil {
			log.Error("connect postgres", "error", err)
			os.Exit(1)
		}
		defer st.Close()
		ddl, err := os.ReadFile("migrations/0001_init.sql")
		if err != nil {
			log.Error("read migration", "error", err)
			os.Exit(1)
		}
		if err := st.Migrate(ctx, string(ddl)); err != nil {
			log.Error("migrate", "error", err)
			os.Exit(1)
		}
		log.Info("postgres audit enabled")
	} else {
		log.Warn("GATEWAY_DATABASE_DSN unset: audit persistence disabled")
	}

	lis, err := net.Listen("tcp", *addr)
	if err != nil {
		log.Error("listen", "error", err)
		os.Exit(1)
	}
	grpcServer := grpc.NewServer()
	gatewayv1.RegisterGatewayServer(grpcServer, server.New(reg, st, *bufSize, log))
	reflection.Register(grpcServer)

	errCh := make(chan error, 1)
	go func() {
		log.Info("gateway listening", "addr", lis.Addr().String())
		errCh <- grpcServer.Serve(lis)
	}()

	select {
	case <-ctx.Done():
		log.Info("shutting down")
		done := make(chan struct{})
		go func() { grpcServer.GracefulStop(); close(done) }()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			grpcServer.Stop()
		}
	case err := <-errCh:
		if err != nil && !errors.Is(err, grpc.ErrServerStopped) {
			log.Error("serve", "error", err)
			os.Exit(1)
		}
	}
	fmt.Fprintln(os.Stderr, "gateway stopped")
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
