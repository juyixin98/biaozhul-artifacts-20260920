// Package store 是 PostgreSQL 持久化层。
// 只暴露语义化方法；所有环境指针切换都在单事务内以“代次 CAS”完成。
package store

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

type DB struct {
	Pool *pgxpool.Pool
}

func Open(ctx context.Context, dsn string) (*DB, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, err
	}
	cfg.MaxConns = 10
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, err
	}
	pingCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := pool.Ping(pingCtx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping database: %w", err)
	}
	return &DB{Pool: pool}, nil
}

func (d *DB) Close() { d.Pool.Close() }

// Migrate 按文件名顺序执行 migrations 目录中尚未执行的 .sql（用 schema_migrations 记账）。
func (d *DB) Migrate(ctx context.Context, dir string) error {
	_, err := d.Pool.Exec(ctx, `CREATE TABLE IF NOT EXISTS schema_migrations (
		filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())`)
	if err != nil {
		return err
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	var names []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".sql") {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	for _, name := range names {
		var ok bool
		if err := d.Pool.QueryRow(ctx,
			`SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE filename=$1)`, name).Scan(&ok); err != nil {
			return err
		}
		if ok {
			continue
		}
		sqlBytes, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			return err
		}
		tx, err := d.Pool.BeginTx(ctx, pgx.TxOptions{})
		if err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, string(sqlBytes)); err != nil {
			tx.Rollback(ctx)
			return fmt.Errorf("migration %s: %w", name, err)
		}
		if _, err := tx.Exec(ctx, `INSERT INTO schema_migrations(filename) VALUES($1)`, name); err != nil {
			tx.Rollback(ctx)
			return err
		}
		if err := tx.Commit(ctx); err != nil {
			return err
		}
	}
	return nil
}

// ErrNotFound 统一“查不到”
var ErrNotFound = errors.New("not found")

// ErrConflict 代次 CAS 失败
var ErrConflict = errors.New("environment generation conflict")

// ErrAlreadyExists 唯一约束冲突（语义化包装）
var ErrAlreadyExists = errors.New("already exists")

// DSNFromEnv 组装 DSN，默认值面向本机开发
func DSNFromEnv() string {
	if v := os.Getenv("DATABASE_URL"); v != "" {
		return v
	}
	q := url.Values{}
	q.Set("sslmode", envOr("PGSSLMODE", "disable"))
	u := url.URL{
		Scheme:   "postgres",
		User:     url.UserPassword(envOr("PGUSER", "promo"), envOr("PGPASSWORD", "promo_dev_pwd")),
		Host:     envOr("PGHOST", "127.0.0.1") + ":" + envOr("PGPORT", "5432"),
		Path:     envOr("PGDATABASE", "promo_atomic"),
		RawQuery: q.Encode(),
	}
	return u.String()
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// isUniqueViolation 判断是否 Postgres 唯一约束冲突 (SQLSTATE 23505)
func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code == "23505"
	}
	return false
}
