// Command gateway runs the protocol-version compatibility gRPC gateway.
package main

import (
	"context"
	"flag"
	"log"
	"net"
	"os"
	"os/signal"
	"syscall"

	"google.golang.org/grpc"

	gatewayv1 "github.com/example/compgw/gen/gateway/v1"
	"github.com/example/compgw/internal/config"
	"github.com/example/compgw/internal/mapping"
	"github.com/example/compgw/internal/server"
	"github.com/example/compgw/internal/store"
)

func main() {
	cfg, err := config.FromEnv()
	if err != nil {
		log.Fatalf("config: %v", err)
	}
	addr := flag.String("addr", cfg.GRPCAddr, "listen address")
	flag.Parse()

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	log.Printf("connecting postgres: %s", cfg.PostgresDSN)
	st, err := store.New(ctx, cfg.PostgresDSN)
	if err != nil {
		log.Fatalf("postgres: %v", err)
	}
	defer st.Close()

	if err := st.Migrate(ctx); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	if cfg.Seed {
		if err := st.Seed(ctx, mapping.SeedSpecs()); err != nil {
			log.Fatalf("seed: %v", err)
		}
		log.Print("mapping seeds ensured (strict-v1-v2, strict-v2-v1, lenient-v2-v1)")
	}

	reg := server.NewMappingRegistry(st)
	if err := reg.Load(ctx); err != nil {
		log.Fatalf("load mappings: %v", err)
	}
	go reg.Watch(ctx)

	lis, err := net.Listen("tcp", *addr)
	if err != nil {
		log.Fatalf("listen %s: %v", *addr, err)
	}
	gs := grpc.NewServer()
	gatewayv1.RegisterCompatGatewayServer(gs, server.New(reg, st, cfg.MaxInFlight))

	go func() {
		<-ctx.Done()
		log.Print("shutdown requested; stopping conversions")
		gs.GracefulStop()
	}()

	log.Printf("compat gateway listening on %s (max_in_flight=%d)", *addr, cfg.MaxInFlight)
	if err := gs.Serve(lis); err != nil {
		log.Printf("grpc serve: %v", err)
		os.Exit(1)
	}
	log.Print("stopped")
}
