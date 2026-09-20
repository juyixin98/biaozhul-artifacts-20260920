package config

import (
	"os"
)

type Config struct {
	DatabaseURL string
	Addr        string
}

func Load() Config {
	c := Config{
		DatabaseURL: os.Getenv("DATABASE_URL"),
		Addr:        os.Getenv("ADDR"),
	}
	if c.DatabaseURL == "" {
		c.DatabaseURL = "postgres://signalboard:signalboard@localhost:5432/signalboard?sslmode=disable"
	}
	if c.Addr == "" {
		c.Addr = ":8080"
	}
	return c
}
