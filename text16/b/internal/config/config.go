package config

import (
	"fmt"
	"os"
	"strings"
)

// Config 保存服务运行配置。所有敏感配置均通过环境变量注入。
type Config struct {
	// HTTPAddr HTTP 监听地址。
	HTTPAddr string
	// DBDriver 数据库驱动：mysql 或 sqlite（sqlite 仅用于本地测试）。
	DBDriver string
	// DSN 数据库连接串。
	DSN string
	// ChunkSize 分块哈希块大小（字节）。
	ChunkSize int
	// JWTSecret 令牌签名密钥。
	JWTSecret []byte
	// TokenTTLHours 令牌有效期（小时）。
	TokenTTLHours int
	// Roots 白名单根目录，name -> 宿主机路径。
	Roots map[string]string
	// Users 本地用户表，username -> {password, role}。
	Users map[string]User
}

// User 是本地静态账号。
type User struct {
	Password string
	Role     string
}

const (
	RoleInvestigator = "investigator" // 调查员：登记、移交、查询、备注
	RoleAnalyst      = "analyst"      // 分析师：查询、备注
)

// Load 从环境变量读取配置。
func Load() (*Config, error) {
	cfg := &Config{
		HTTPAddr:      getenv("FORENSIC_HTTP_ADDR", ":8080"),
		DBDriver:      getenv("FORENSIC_DB_DRIVER", "mysql"),
		DSN:           getenv("FORENSIC_DB_DSN", "forensic:forensic@tcp(mysql:3306)/forensic?charset=utf8mb4&parseTime=True&loc=Local"),
		ChunkSize:     getenvInt("FORENSIC_CHUNK_SIZE", 4*1024*1024),
		TokenTTLHours: getenvInt("FORENSIC_TOKEN_TTL_HOURS", 12),
		Roots:         map[string]string{},
		Users:         map[string]User{},
	}

	secret := getenv("FORENSIC_JWT_SECRET", "forensiccore-dev-secret-change-me")
	cfg.JWTSecret = []byte(secret)

	// 白名单根目录：name=/path;name2=/path2
	roots := getenv("FORENSIC_ROOTS", "/data/evidence")
	for _, item := range strings.Split(roots, ";") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		var name, path string
		if i := strings.Index(item, "="); i >= 0 {
			name = strings.TrimSpace(item[:i])
			path = strings.TrimSpace(item[i+1:])
		} else {
			// 未显式命名时，取目录基名作为逻辑名。
			path = item
			name = baseName(path)
		}
		if name == "" || path == "" {
			return nil, fmt.Errorf("invalid root entry %q", item)
		}
		cfg.Roots[name] = path
	}
	if len(cfg.Roots) == 0 {
		return nil, fmt.Errorf("FORENSIC_ROOTS must contain at least one whitelisted directory")
	}

	// 本地账号：username:password:role;...
	users := getenv("FORENSIC_USERS", "investigator:invest123:investigator;analyst:analyst123:analyst")
	for _, item := range strings.Split(users, ";") {
		parts := strings.Split(item, ":")
		if len(parts) != 3 {
			return nil, fmt.Errorf("invalid user entry %q, want username:password:role", item)
		}
		role := strings.TrimSpace(parts[2])
		if role != RoleInvestigator && role != RoleAnalyst {
			return nil, fmt.Errorf("invalid role %q for user %q", role, parts[0])
		}
		cfg.Users[strings.TrimSpace(parts[0])] = User{
			Password: strings.TrimSpace(parts[1]),
			Role:     role,
		}
	}
	if len(cfg.Users) == 0 {
		return nil, fmt.Errorf("FORENSIC_USERS must contain at least one account")
	}

	if cfg.ChunkSize < 4096 {
		return nil, fmt.Errorf("FORENSIC_CHUNK_SIZE must be >= 4096")
	}
	return cfg, nil
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func getenvInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		var n int
		if _, err := fmt.Sscanf(v, "%d", &n); err != nil || n <= 0 {
			return def
		}
		return n
	}
	return def
}

func baseName(p string) string {
	p = strings.TrimRight(p, "/")
	if i := strings.LastIndex(p, "/"); i >= 0 {
		return p[i+1:]
	}
	return p
}
