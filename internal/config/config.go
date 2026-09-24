// Package config holds gateway runtime configuration.
package config

import (
	"fmt"
	"os"
	"strconv"
)

type Config struct {
	GRPCAddr    string
	PostgresDSN string
	MaxInFlight int
	Seed        bool
}

func FromEnv() (Config, error) {
	c := Config{
		GRPCAddr:    getenv("COMPGW_GRPC_ADDR", "127.0.0.1:50051"),
		PostgresDSN: getenv("COMPGW_PG_DSN", "postgres://compgw:compgw@localhost:5432/compgw?sslmode=disable"),
		MaxInFlight: 64,
		Seed:        true,
	}
	if v := os.Getenv("COMPGW_MAX_IN_FLIGHT"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n <= 0 {
			return c, fmt.Errorf("COMPGW_MAX_IN_FLIGHT must be a positive integer, got %q", v)
		}
		c.MaxInFlight = n
	}
	if v := os.Getenv("COMPGW_SEED"); v == "false" || v == "0" {
		c.Seed = false
	}
	return c, nil
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
