// Command server 启动可撤销凭证索引服务。
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

	"revcred/internal/api"
	"revcred/internal/config"
	"revcred/internal/core"
	"revcred/internal/store"
	"revcred/migrations"
)

func main() {
	cfg := config.FromEnv()
	ctx := context.Background()

	st, err := store.New(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("store: %v", err)
	}
	defer st.Close()

	if err := st.Migrate(ctx, migrations.FS); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	log.Printf("migrations applied")

	svc := core.NewService(st)
	srv := &http.Server{
		Addr:              cfg.Addr,
		Handler:           api.NewRouter(svc),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		log.Printf("listening on %s", cfg.Addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Fatalf("shutdown: %v", err)
	}
	log.Printf("stopped")
}
