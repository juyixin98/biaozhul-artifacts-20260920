// Package config 从环境变量读取服务配置。
package config

import "os"

// Config 是服务运行所需的全部配置。
type Config struct {
	// Addr 是 HTTP 监听地址，默认 :8080。
	Addr string
	// DatabaseURL 是 PostgreSQL 连接串。
	DatabaseURL string
}

// FromEnv 读取环境变量并填充默认值。
func FromEnv() Config {
	addr := os.Getenv("ADDR")
	if addr == "" {
		addr = ":8080"
	}
	db := os.Getenv("DATABASE_URL")
	if db == "" {
		db = "postgres://revcred:revcred@localhost:55433/revcred?sslmode=disable"
	}
	return Config{Addr: addr, DatabaseURL: db}
}
