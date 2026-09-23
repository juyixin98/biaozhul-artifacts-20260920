// Command vci-server runs the revocable credential index HTTP API.
//
// All identities and keys created by this server are SYNTHETIC TEST DATA.
package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"vci/internal/clock"
	"vci/internal/httpapi"
	"vci/internal/service"
	"vci/internal/store"
)

func main() {
	addr := env("VCI_ADDR", ":8090")
	dbURL := env("DATABASE_URL", "postgres://vc_test:vc_test@localhost:5432/vc_index?sslmode=disable")

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	st, err := store.New(ctx, dbURL)
	if err != nil {
		log.Fatalf("connect postgres: %v", err)
	}
	defer st.Close()

	pingCtx, pCancel := context.WithTimeout(ctx, 10*time.Second)
	defer pCancel()
	if err := st.Ping(pingCtx); err != nil {
		log.Fatalf("ping postgres: %v", err)
	}
	if err := st.Migrate(ctx); err != nil {
		log.Fatalf("migrate: %v", err)
	}

	svc := service.New(st, clock.System{})
	srv := &http.Server{
		Addr:              addr,
		Handler:           httpapi.NewServer(svc).Router(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("vci-server listening on %s (synthetic test identities only)", addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http server: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("shutting down…")
	shCtx, shCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shCancel()
	if err := srv.Shutdown(shCtx); err != nil {
		log.Printf("shutdown: %v", err)
	}
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
