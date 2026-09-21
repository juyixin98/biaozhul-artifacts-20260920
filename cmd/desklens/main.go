package main

import (
	"log"

	_ "time/tzdata" // IANA zones (Europe/London, ...) on minimal Alpine images

	"github.com/labstack/echo/v4"
	"github.com/labstack/echo/v4/middleware"

	"desklens/internal/aggregate"
	"desklens/internal/api"
	"desklens/internal/config"
	"desklens/internal/database"
	"desklens/internal/ingest"
	"desklens/internal/repo"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	db, err := database.Connect(cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("database connect: %v", err)
	}
	defer db.Close()

	if err := database.Migrate(db); err != nil {
		log.Fatalf("migrate: %v", err)
	}
	log.Print("migrations applied")

	r := repo.New(db)
	e := echo.New()
	e.HideBanner = true
	e.Use(middleware.Logger())
	e.Use(middleware.Recover())

	h := api.NewHandlers(r, ingest.NewService(r), aggregate.NewService(r))
	h.Register(e)

	log.Printf("DeskLens listening on %s", cfg.Addr)
	if err := e.Start(cfg.Addr); err != nil {
		log.Fatalf("server: %v", err)
	}
}
