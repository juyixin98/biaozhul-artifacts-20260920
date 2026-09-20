package config

import (
	"os"
	"strconv"
)

type Config struct {
	Addr        string // HTTP 监听地址
	DSN         string // MySQL DSN
	StorageRoot string // 文件存储根目录（本地磁盘）
	MaxUploadMB int64  // 单文件大小上限（MB）
	SeedDemo    bool   // 是否写入演示用户
}

func Load() Config {
	return Config{
		Addr:        env("ADDR", ":8080"),
		DSN:         env("DB_DSN", "proofcycle:proofcycle@tcp(127.0.0.1:3306)/proofcycle?charset=utf8mb4&parseTime=true&loc=UTC"),
		StorageRoot: env("STORAGE_ROOT", "./data/files"),
		MaxUploadMB: envInt64("MAX_UPLOAD_MB", 50),
		SeedDemo:    env("SEED_DEMO", "false") == "true",
	}
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt64(key string, def int64) int64 {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil && n > 0 {
			return n
		}
	}
	return def
}
