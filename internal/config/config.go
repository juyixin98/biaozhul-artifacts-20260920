// Package config 从环境变量读取 ProofCycle 运行配置。
package config

import (
	"fmt"
	"os"
	"strconv"
)

// Config 服务运行配置。
type Config struct {
	HTTPAddr      string // 监听地址
	MySQLDSN      string // GORM MySQL DSN
	StorageRoot   string // 本地文件存储根目录
	MaxUploadSize int64  // 单个上传文件大小上限（字节），仅允许 PDF/PNG
	AutoMigrate   bool   // 启动时执行 GORM AutoMigrate
	Seed          bool   // 启动时写入演示数据（幂等）
}

// FromEnv 从环境变量加载配置，未设置时使用适合本地/Docker 演示的默认值。
func FromEnv() Config {
	c := Config{
		HTTPAddr:      getenv("PROOFCYCLE_HTTP_ADDR", ":8080"),
		StorageRoot:   getenv("PROOFCYCLE_STORAGE_ROOT", "/data/files"),
		MaxUploadSize: 20 << 20, // 20 MiB
		AutoMigrate:   getenvBool("PROOFCYCLE_AUTO_MIGRATE", true),
		Seed:          getenvBool("PROOFCYCLE_SEED", true),
	}

	dsn := os.Getenv("PROOFCYCLE_MYSQL_DSN")
	if dsn == "" {
		dsn = fmt.Sprintf(
			"%s:%s@tcp(%s:%s)/%s?charset=utf8mb4&parseTime=true&loc=UTC",
			getenv("MYSQL_USER", "proofcycle"),
			getenv("MYSQL_PASSWORD", "proofcycle"),
			getenv("MYSQL_HOST", "127.0.0.1"),
			getenv("MYSQL_PORT", "3306"),
			getenv("MYSQL_DATABASE", "proofcycle"),
		)
	}
	c.MySQLDSN = dsn

	if v := os.Getenv("PROOFCYCLE_MAX_UPLOAD_BYTES"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil && n > 0 {
			c.MaxUploadSize = n
		}
	}
	return c
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func getenvBool(key string, def bool) bool {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	switch v {
	case "1", "true", "TRUE", "yes":
		return true
	default:
		return false
	}
}
