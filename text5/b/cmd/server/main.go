package main

import (
	"log"
	"os"
	"time"

	"github.com/jmoiron/sqlx"
	_ "github.com/lib/pq"

	"desklens/internal/httpapi"
	"desklens/internal/store"
	"desklens/migrations"
)

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	dsn := env("DATABASE_URL", "postgres://desklens:desklens@localhost:5432/desklens?sslmode=disable")
	addr := env("ADDR", ":8080")

	var db *sqlx.DB
	var err error
	for attempt := 0; attempt < 30; attempt++ {
		db, err = sqlx.Connect("postgres", dsn)
		if err == nil {
			break
		}
		log.Printf("waiting for database: %v", err)
		time.Sleep(time.Second)
	}
	if err != nil {
		log.Fatalf("connect database: %v", err)
	}
	defer db.Close()

	if err := migrations.Apply(db); err != nil {
		log.Fatalf("apply migrations: %v", err)
	}

	if env("SEED_DEMO_DATA", "false") == "true" {
		seed, err := os.ReadFile(env("SEED_FILE", "seed/seed.sql"))
		if err != nil {
			log.Fatalf("read seed file: %v", err)
		}
		if _, err := db.Exec(string(seed)); err != nil {
			log.Fatalf("apply seed: %v", err)
		}
		log.Print("demo seed data applied")
	}

	st := store.New(db)
	e := httpapi.Router(st)
	log.Printf("DeskLens listening on %s", addr)
	if err := e.Start(addr); err != nil {
		log.Fatal(err)
	}
}
