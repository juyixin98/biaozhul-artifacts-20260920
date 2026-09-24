// Command shardmigrator runs the single-primary shard migration simulator.
//
// It boots an in-memory cluster with two nodes (node-a primary, node-b
// replica) and one shard (orders), and serves the HTTP API until SIGINT.
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"shardmigrator/internal/cluster"
	"shardmigrator/internal/httpapi"
)

func main() {
	addr := flag.String("addr", envOr("ADDR", ":8080"), "listen address (env ADDR)")
	flag.Parse()

	cl := cluster.NewCluster()
	must(cl.AddNode("node-a"))
	must(cl.AddNode("node-b"))
	if err := cl.CreateShard("orders", []string{"node-a", "node-b"}); err != nil {
		log.Fatalf("create shard: %v", err)
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           httpapi.NewServer(cl).Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		log.Printf("shard migration simulator listening on %s (shard=orders, primary=node-a, route v1)", *addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutdown signal received")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
	}
}

func must(err error) {
	if err != nil {
		log.Fatal(err)
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
