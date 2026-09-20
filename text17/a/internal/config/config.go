package config

import "os"

type Config struct {
	Addr        string
	DatabaseURL string
	DataDir     string
}

func FromEnv() Config {
	return Config{
		Addr:        env("ADDR", ":8080"),
		DatabaseURL: env("DATABASE_URL", "postgres://postgres:postgres@localhost:5432/synapticgo?sslmode=disable"),
		DataDir:     env("DATA_DIR", "./data"),
	}
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
