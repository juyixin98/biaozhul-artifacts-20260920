package config

import (
	"fmt"
	"os"
)

// Config holds runtime configuration sourced from environment variables.
type Config struct {
	HTTPAddr    string
	MySQLDSN    string
	SeedSamples bool
}

func FromEnv() Config {
	c := Config{
		HTTPAddr:    getenv("HTTP_ADDR", ":8080"),
		MySQLDSN:    "",
		SeedSamples: getenv("SEED_SAMPLES", "true") == "true",
	}
	host := getenv("MYSQL_HOST", "127.0.0.1")
	port := getenv("MYSQL_PORT", "3306")
	user := getenv("MYSQL_USER", "geoterritory")
	pass := getenv("MYSQL_PASSWORD", "geoterritory")
	db := getenv("MYSQL_DATABASE", "geoterritory")
	c.MySQLDSN = fmt.Sprintf(
		"%s:%s@tcp(%s:%s)/%s?charset=utf8mb4&parseTime=true&loc=UTC&multiStatements=true",
		user, pass, host, port, db)
	// MYSQL_DSN overrides the assembled one when set (used by the test suite).
	if dsn := os.Getenv("MYSQL_DSN"); dsn != "" {
		c.MySQLDSN = dsn
	}
	return c
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
