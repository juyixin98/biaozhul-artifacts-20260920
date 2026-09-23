// Command mqttd runs the MQTT 3.1.1 subset broker: a raw TCP listener for
// MQTT clients and, alongside it, an HTTP control API backed by net/http.
package main

import (
	"context"
	"flag"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"mqttsub/internal/broker"
	"mqttsub/internal/httpapi"
)

func main() {
	mqttAddr := flag.String("mqtt", ":1883", "MQTT TCP listen address")
	httpAddr := flag.String("http", ":8081", "HTTP control API listen address")
	storePath := flag.String("store", "mqtt-sessions.json", "durable session file (empty disables persistence)")
	retry := flag.Duration("retry", 10*time.Second, "live QoS1 inflight retry interval (0 = retry only on reconnect)")
	flag.Parse()

	b, err := broker.New(broker.Config{StorePath: *storePath, RetryInterval: *retry})
	if err != nil {
		log.Fatalf("broker init: %v", err)
	}

	ln, err := net.Listen("tcp", *mqttAddr)
	if err != nil {
		log.Fatalf("mqtt listen %s: %v", *mqttAddr, err)
	}
	srv := &http.Server{
		Addr:              *httpAddr,
		Handler:           httpapi.Handler(b),
		ReadHeaderTimeout: 10 * time.Second,
	}
	httpLn, err := net.Listen("tcp", *httpAddr)
	if err != nil {
		log.Fatalf("http listen %s: %v", *httpAddr, err)
	}

	log.Printf("mqttd: MQTT on %s  HTTP on %s  store=%q  retry=%s",
		*mqttAddr, *httpAddr, *storePath, *retry)

	go func() {
		if err := srv.Serve(httpLn); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http serve: %v", err)
		}
	}()

	go func() {
		for {
			nc, err := ln.Accept()
			if err != nil {
				return
			}
			go b.Serve(nc)
		}
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh
	log.Printf("mqttd: shutting down")

	// Stop taking new connections and close live MQTT clients.
	_ = ln.Close()
	b.CloseAllConns()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
	if err := b.Shutdown(); err != nil {
		log.Printf("mqttd: final session flush failed: %v", err)
	}
	log.Printf("mqttd: stopped")
}
