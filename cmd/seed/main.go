// Command seed provisions devices (id + HMAC secret) in PostgreSQL.
//
//	seed -id dev-001 [-secret secret-dev-001]
//
// Default secret is "secret-<id>", matching cmd/device and cmd/inject defaults.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	"mqttredel/internal/store"
)

func main() {
	var (
		pgDSN    = flag.String("pg", env("PG_DSN", "postgres://mqttredel:mqttredel@127.0.0.1:55433/mqttredel?sslmode=disable"), "Postgres DSN")
		deviceID = flag.String("id", "", "device id to provision")
		secret   = flag.String("secret", "", "HMAC secret (default: secret-<id>)")
	)
	flag.Parse()
	if *deviceID == "" {
		fmt.Fprintln(os.Stderr, "-id is required")
		os.Exit(2)
	}
	if *secret == "" {
		*secret = "secret-" + *deviceID
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := store.Open(ctx, *pgDSN); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	defer store.Close()
	if err := store.SeedDevice(ctx, *deviceID, *secret); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	fmt.Printf("seeded device %q with secret %q\n", *deviceID, *secret)
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
