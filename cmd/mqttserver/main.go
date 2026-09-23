// Command mqttserver runs the MQTT 3.1.1 subset broker (raw TCP) together
// with its HTTP inspection/management API. It uses only the Go standard
// library.
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"mqttsubset/httpapi"
	"mqttsubset/mqtt"
)

func main() {
	mqttAddr := flag.String("mqtt", ":1883", "listen address for MQTT TCP clients")
	httpAddr := flag.String("http", ":8080", "listen address for the HTTP API")
	stateDir := flag.String("state", "./state", "directory for the durable session snapshot")
	flag.Parse()

	logger := log.New(os.Stdout, "", log.LstdFlags)

	ln, err := net.Listen("tcp", *mqttAddr)
	if err != nil {
		logger.Fatalf("MQTT listen %s: %v", *mqttAddr, err)
	}
	broker, err := mqtt.NewBroker(*stateDir, logger)
	if err != nil {
		logger.Fatalf("broker init: %v", err)
	}

	go func() {
		logger.Printf("MQTT 3.1.1 subset listening on %s (state: %s)", ln.Addr(), *stateDir)
		if err := broker.Serve(ln); err != nil {
			logger.Printf("MQTT server stopped: %v", err)
		}
	}()

	srv := &http.Server{
		Addr:              *httpAddr,
		Handler:           httpapi.Handler(broker, logger),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		logger.Printf("HTTP API listening on %s", *httpAddr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Fatalf("HTTP server: %v", err)
		}
	}()

	sigc := make(chan os.Signal, 1)
	signal.Notify(sigc, syscall.SIGINT, syscall.SIGTERM)
	<-sigc
	logger.Printf("shutting down")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
	_ = broker.Close()
	logger.Printf("stopped")
}
