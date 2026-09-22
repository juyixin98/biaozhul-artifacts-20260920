// Standalone migration runner: applies embedded SQL and exits.
package main

import (
	"context"
	"fmt"
	"log"
	"os"

	"dams/internal/platform/dbpool"
	"dams/internal/platform/migrate"
)

func main() {
	dsn := os.Getenv("DAMS_DATABASE_URL")
	if dsn == "" {
		dsn = "postgres://dams:dams@localhost:5432/dams?sslmode=disable"
	}
	ctx := context.Background()
	pool, err := dbpool.New(ctx, dsn)
	if err != nil {
		log.Fatalf("connect db: %v", err)
	}
	defer pool.Close()
	n, err := migrate.Up(ctx, pool)
	if err != nil {
		log.Fatalf("migrate: %v", err)
	}
	fmt.Printf("migrations applied: %d\n", n)
}
