// Command modbus-server runs the Modbus TCP server with SQLite-backed
// write receipts.
//
// Usage:
//
//	modbus-server -listen :1502 -db receipts.db -registers 100 \
//	    -key-file hmac.key
//
// The HMAC key may instead be supplied through MODBUS_RECEIPT_KEY.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"modbus-receipt/internal/receipt"
	"modbus-receipt/internal/register"
	"modbus-receipt/internal/server"
)

func main() {
	listen := flag.String("listen", ":1502", "TCP listen address")
	dbPath := flag.String("db", "receipts.db", "SQLite database file")
	nReg := flag.Int("registers", 100, "number of holding registers (addresses 0..N-1)")
	units := flag.String("units", "1", "comma-separated unit ids to serve")
	keyFile := flag.String("key-file", "", "file containing the HMAC-SHA256 receipt key")
	idle := flag.Duration("idle-timeout", 2*time.Minute, "close connections idle for this long (0 = never)")
	flag.Parse()

	logger := log.New(os.Stderr, "modbus-server ", log.LstdFlags|log.Lmicroseconds)

	key, err := receipt.LoadKey(*keyFile)
	if err != nil {
		fatal(logger, "key error: %v", err)
	}
	if len(key) == 0 {
		fatal(logger, "no HMAC key: provide -key-file or set MODBUS_RECEIPT_KEY (see README)")
	}

	allowed, err := parseUnits(*units)
	if err != nil {
		fatal(logger, "%v", err)
	}

	rs, err := receipt.Open(*dbPath, key)
	if err != nil {
		fatal(logger, "open database %s: %v", *dbPath, err)
	}
	defer rs.Close()

	store := register.New(*nReg)
	srv, err := server.New(server.Config{
		Listen:       *listen,
		Registers:    *nReg,
		AllowedUnits: allowed,
		IdleTimeout:  *idle,
	}, store, rs, logger)
	if err != nil {
		fatal(logger, "%v", err)
	}
	if err := srv.Listen(); err != nil {
		fatal(logger, "listen %s: %v", *listen, err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	errCh := make(chan error, 1)
	go func() { errCh <- srv.Serve(ctx) }()

	sig := <-ctx.Done()
	logger.Printf("signal received (%v), shutting down", sig)
	if err := <-errCh; err != nil {
		logger.Printf("server error: %v", err)
		os.Exit(1)
	}
}

func parseUnits(s string) ([]byte, error) {
	var out []byte
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		n, err := strconv.Atoi(part)
		if err != nil || n < 0 || n > 255 {
			return nil, fmt.Errorf("invalid unit id %q", part)
		}
		out = append(out, byte(n))
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("at least one unit id required")
	}
	return out, nil
}

func fatal(logger *log.Logger, format string, a ...any) {
	logger.Printf(format, a...)
	os.Exit(1)
}
