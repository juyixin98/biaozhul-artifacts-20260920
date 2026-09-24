// Package config loads service configuration from the environment.
package config

import "os"

type Config struct {
	DatabaseURL string
	BlobRoot    string
	ListenAddr  string
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func Load() Config {
	return Config{
		DatabaseURL: getenv("DATABASE_URL", "postgres://promote:promote@localhost:5432/promotion_b?sslmode=disable"),
		BlobRoot:    getenv("BLOB_ROOT", "./data/blobs"),
		ListenAddr:  getenv("LISTEN_ADDR", ":8080"),
	}
}
