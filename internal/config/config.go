package config

import (
	"os"
	"strconv"
	"time"
)

// Config 是进程级配置。所有配置均来自环境变量，默认值保证开箱即用。
type Config struct {
	HTTPAddr string

	DBHost string
	DBPort string
	DBName string
	DBUser string
	DBPass string

	JWTSecret []byte
	JWTTTL    time.Duration

	// BackfillWindow：允许补算（乱序/延迟上报）的最早发生时间范围。
	// FutureSkew：允许的未来时间时钟偏差。
	// 合法事件发生时间窗口为 [now-BackfillWindow, now+FutureSkew]。
	BackfillWindow time.Duration
	FutureSkew     time.Duration
	BatchMaxEvents int

	SchedulerEnabled bool
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func getdur(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}

func Load() Config {
	c := Config{
		HTTPAddr: getenv("APP_HTTP_ADDR", ":8080"),

		DBHost: getenv("DB_HOST", "127.0.0.1"),
		DBPort: getenv("DB_PORT", "3306"),
		DBName: getenv("DB_NAME", "activityguard"),
		DBUser: getenv("DB_USER", "guard"),
		DBPass: getenv("DB_PASSWORD", "guardpw_change_me"),

		JWTSecret: []byte(getenv("JWT_SECRET", "dev-secret-do-not-use-in-production")),
		JWTTTL:    getdur("JWT_TTL", 12*time.Hour),

		// 默认允许补算最近 30 天，与统计规则窗口一致，便于演示与补录。
		BackfillWindow: getdur("BACKFILL_WINDOW", 30*24*time.Hour),
		FutureSkew:     getdur("FUTURE_SKEW", 5*time.Minute),
		BatchMaxEvents: 2000,

		SchedulerEnabled: getenv("SCHEDULER_ENABLED", "true") != "false",
	}
	if n, err := strconv.Atoi(os.Getenv("BATCH_MAX_EVENTS")); err == nil && n > 0 {
		c.BatchMaxEvents = n
	}
	return c
}

// DSN 返回 MySQL 驱动连接串。parseTime 让 DATETIME 列以 time.Time 返回；
// loc=UTC 意味着读回的时间一律按 UTC 解释（数据库本身也以 +00:00 运行）。
func (c Config) DSN() string {
	return c.DBUser + ":" + c.DBPass +
		"@tcp(" + c.DBHost + ":" + c.DBPort + ")/" + c.DBName +
		"?charset=utf8mb4&parseTime=true&loc=UTC&multiStatements=false"
}
