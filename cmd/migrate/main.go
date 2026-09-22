// Command migrate applies all embedded SQL migrations and exits.
package main

import (
	"context"
	"log"

	"github.com/jackc/pgx/v5"

	"community-governance/internal/config"
	"community-governance/internal/migrate"
)

func main() {
	cfg := config.Load()
	ctx := context.Background()
	conn, err := pgx.Connect(ctx, cfg.Database)
	if err != nil {
		log.Fatalf("connect: %v", err)
	}
	defer conn.Close(ctx)
	if err := migrate.Migrate(ctx, conn); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	log.Print("migrations applied")
}
