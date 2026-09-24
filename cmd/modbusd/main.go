// Command modbusd is the Modbus/TCP gateway server.
package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"

	"modbus-receipt/internal/server"
	"modbus-receipt/internal/storage"
)

func main() {
	listen := flag.String("listen", ":1502",
		"listen address (Modbus/TCP's privileged default is 502; 1502 needs no root)")
	dbPath := flag.String("db", "data/modbus.db", "SQLite database file (WAL files created alongside)")
	bankSize := flag.Int("bank", 1000, "number of holding registers (addresses 0..bank-1)")
	units := flag.String("units", "1", "comma-separated list of accepted Unit IDs, e.g. '1,2'")
	idleTimeout := flag.Duration("idle-timeout", 0, "close idle connections after this duration (0 = never)")
	flag.Parse()

	allowed, err := parseUnits(*units)
	if err != nil {
		log.Fatal(err)
	}
	if err := os.MkdirAll(dbDir(*dbPath), 0o755); err != nil {
		log.Fatalf("create db directory: %v", err)
	}

	logger := log.New(os.Stdout, "modbusd ", log.LstdFlags|log.Lmicroseconds)

	store, err := storage.NewStore(context.Background(), storage.Options{
		DSN:          "file:" + *dbPath + "?_txlock=immediate",
		BankSize:     *bankSize,
		AllowedUnits: allowed,
	})
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer store.Close()

	srv := server.New(server.Config{
		ListenAddr:  *listen,
		Store:       store,
		ReadTimeout: *idleTimeout,
	}, logger)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		<-ctx.Done()
		logger.Printf("shutdown signal received")
		srv.Shutdown()
	}()

	if err := srv.Serve(ctx); err != nil {
		log.Fatal(err)
	}
	logger.Printf("stopped")
}

func dbDir(path string) string {
	for i := len(path) - 1; i >= 0; i-- {
		if os.IsPathSeparator(path[i]) {
			if i == 0 {
				return "/"
			}
			return path[:i]
		}
	}
	return "."
}
