// Command metricrollup runs the HTTP ingestion/query server.
package main

import (
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"metricrollup/internal/server"
	"metricrollup/internal/store"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	dataFile := flag.String("data", "./data/snapshot.json", "snapshot file (empty disables load/save)")
	flag.Parse()

	logger := log.New(os.Stdout, "rollup ", log.LstdFlags|log.Lmicroseconds)
	st := store.New()
	if *dataFile != "" {
		if err := st.Load(*dataFile); err != nil {
			logger.Fatalf("load snapshot %s: %v", *dataFile, err)
		}
		logger.Printf("snapshot loaded from %s", *dataFile)
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           server.New(st, *dataFile, logger).Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		logger.Printf("listening on %s", *addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Fatalf("server: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	logger.Print("shutting down")
}
