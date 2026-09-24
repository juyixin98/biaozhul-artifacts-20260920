// Command consumer is the backend service: it subscribes to telemetry/# with a
// durable MQTT session, validates and cryptographically verifies every sample,
// and commits raw message + business state + dedup key in ONE Postgres
// transaction before sending PUBACK.
package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"mqttredel/internal/broker"
	"mqttredel/internal/broker/admin"
	"mqttredel/internal/store"
)

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func main() {
	var (
		mqttAddr = flag.String("mqtt", getenv("MQTT_ADDR", "127.0.0.1:11883"), "MQTT broker host:port")
		pgDSN    = flag.String("pg", getenv("PG_DSN", "postgres://mqttredel:mqttredel@127.0.0.1:55433/mqttredel?sslmode=disable"), "Postgres DSN")
		httpAddr = flag.String("http", getenv("HTTP_ADDR", "127.0.0.1:8079"), "admin HTTP listen address")
		clientID = flag.String("client-id", getenv("MQTT_CLIENT_ID", "telemetry-consumer-1"), "fixed MQTT client id (durable session)")
		topic    = flag.String("topic", getenv("MQTT_TOPIC", "telemetry/#"), "subscription topic filter")
	)
	flag.Parse()

	logger := log.New(os.Stdout, "consumer ", log.LstdFlags|log.Lmicroseconds)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	if err := store.Open(ctx, *pgDSN); err != nil {
		logger.Fatalf("postgres: %v", err)
	}
	cancel()
	defer store.Close()
	logger.Printf("postgresql ready; schema applied")

	faults := broker.NewFaults()
	c := broker.NewConsumer(broker.Config{
		Server: *mqttAddr, ClientID: *clientID, Topic: *topic,
	}, logger, faults)

	srv := admin.New(*httpAddr, c, faults, 60*time.Second)
	if err := srv.Start(); err != nil {
		logger.Fatal(err)
	}
	logger.Printf("admin API on http://%s", *httpAddr)

	runCtx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		if err := c.Run(runCtx); err != nil && runCtx.Err() == nil {
			logger.Fatalf("consumer stopped: %v", err)
		}
	}()

	<-runCtx.Done()
	logger.Printf("shutting down")
	c.Close()
	shutdownCtx, c2 := context.WithTimeout(context.Background(), 5*time.Second)
	defer c2()
	_ = srv.Close(shutdownCtx)
}
