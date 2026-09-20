package config

import "os"

type Config struct {
	DatabaseURL string
	ListenAddr  string
}

func Load() Config {
	return Config{
		DatabaseURL: getenv("DATABASE_URL", "postgres://desklens:desklens@localhost:5432/desklens?sslmode=disable"),
		ListenAddr:  getenv("LISTEN_ADDR", ":8080"),
	}
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
