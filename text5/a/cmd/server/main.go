package main

import (
	"context"
	"log"

	"desklens/internal/api"
	"desklens/internal/config"
	"desklens/internal/db"
	"desklens/migrations"
	"desklens/seed"
)

func main() {
	cfg := config.Load()
	ctx := context.Background()

	database, err := db.Connect(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("connect: %v", err)
	}
	defer database.Close()

	if err := db.Migrate(ctx, database, migrations.FS); err != nil {
		log.Fatalf("migrate: %v", err)
	}

	// Load sample data on first start so the compose stack is usable
	// immediately. Idempotent: skipped once any department exists.
	var empty bool
	if err := database.GetContext(ctx, &empty,
		`SELECT NOT EXISTS (SELECT 1 FROM departments)`); err != nil {
		log.Fatalf("probe seed state: %v", err)
	}
	if empty {
		body, err := seed.FS.ReadFile("seed.sql")
		if err != nil {
			log.Fatalf("read seed: %v", err)
		}
		if _, err := database.ExecContext(ctx, string(body)); err != nil {
			log.Fatalf("seed: %v", err)
		}
		log.Print("sample data loaded")
	}

	log.Printf("desklens listening on %s", cfg.ListenAddr)
	if err := api.NewServer(database).Start(cfg.ListenAddr); err != nil {
		log.Fatal(err)
	}
}
