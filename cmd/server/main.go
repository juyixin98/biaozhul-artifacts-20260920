// Command orset-server runs one OR-Set replica over HTTP.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"orset/internal/orset"
	"orset/internal/server"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	id := flag.String("id", "", "replica id (must be cluster-unique; default hostname-pid)")
	flag.Parse()

	replicaID := *id
	if replicaID == "" {
		host, _ := os.Hostname()
		replicaID = fmt.Sprintf("%s-%d", host, os.Getpid())
	}

	logger := log.New(os.Stdout, "[orset "+replicaID+"] ", log.LstdFlags|log.Lmsgprefix)
	set := orset.New(replicaID)
	srv := server.New(set, logger)

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		logger.Printf("replica %q listening on %s", replicaID, *addr)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Fatalf("listen: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	logger.Printf("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_ = httpSrv.Shutdown(ctx)
}
