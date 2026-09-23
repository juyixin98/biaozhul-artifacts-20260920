// Package store 封装 PostgreSQL 存取。所有写操作只追加，不修改历史。
package store

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"sort"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"revcred/internal/core"
)

// Store 是 PostgreSQL 存取层，实现 core.Store 接口。
type Store struct {
	pool *pgxpool.Pool
}

// New 建立连接池并验证连通性。
func New(ctx context.Context, databaseURL string) (*Store, error) {
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		return nil, fmt.Errorf("connect: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping: %w", err)
	}
	return &Store{pool: pool}, nil
}

// Close 关闭连接池。
func (s *Store) Close() { s.pool.Close() }

// Pool 暴露底层连接池（供测试做底层校验）。
func (s *Store) Pool() *pgxpool.Pool { return s.pool }

// Migrate 按文件名顺序应用 migrations 目录下的全部 SQL（均为幂等语句）。
// 多个进程可能同时启动，用 PostgreSQL 咨询锁串行化迁移，
// 避免并发 CREATE TABLE IF NOT EXISTS 的目录竞态。
func (s *Store) Migrate(ctx context.Context, migrations fs.FS) error {
	names, err := fs.Glob(migrations, "*.sql")
	if err != nil {
		return err
	}
	sort.Strings(names)

	conn, err := s.pool.Acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()
	if _, err := conn.Exec(ctx, `SELECT pg_advisory_lock(72727201)`); err != nil {
		return err
	}
	defer conn.Exec(context.Background(), `SELECT pg_advisory_unlock(72727201)`)

	for _, name := range names {
		sql, err := fs.ReadFile(migrations, name)
		if err != nil {
			return err
		}
		if _, err := conn.Exec(ctx, string(sql)); err != nil {
			return fmt.Errorf("migrate %s: %w", name, err)
		}
	}
	return nil
}

// InsertKey 写入一把新密钥。
func (s *Store) InsertKey(ctx context.Context, k *core.Key) error {
	_, err := s.pool.Exec(ctx, `
		INSERT INTO issuer_keys (kid, issuer, public_key, secret_key, valid_from, valid_to)
		VALUES ($1, $2, $3, $4, $5, $6)`,
		k.Kid, k.Issuer, k.PublicKey, k.SecretKey, k.ValidFrom, k.ValidTo)
	return err
}

// CloseActiveKeys 把该签发者当前有效的密钥区间封闭到 at（轮换的前半步）。
func (s *Store) CloseActiveKeys(ctx context.Context, issuer string, at time.Time) error {
	_, err := s.pool.Exec(ctx, `
		UPDATE issuer_keys SET valid_to = $2
		WHERE issuer = $1 AND valid_to IS NULL`, issuer, at)
	return err
}

// GetKey 按 kid 读取密钥。
func (s *Store) GetKey(ctx context.Context, kid string) (*core.Key, error) {
	k := &core.Key{}
	err := s.pool.QueryRow(ctx, `
		SELECT kid, issuer, public_key, secret_key, valid_from, valid_to
		FROM issuer_keys WHERE kid = $1`, kid).
		Scan(&k.Kid, &k.Issuer, &k.PublicKey, &k.SecretKey, &k.ValidFrom, &k.ValidTo)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, core.ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return k, nil
}

// ActiveKey 返回该签发者当前有效（valid_to IS NULL）的密钥。
func (s *Store) ActiveKey(ctx context.Context, issuer string) (*core.Key, error) {
	k := &core.Key{}
	err := s.pool.QueryRow(ctx, `
		SELECT kid, issuer, public_key, secret_key, valid_from, valid_to
		FROM issuer_keys
		WHERE issuer = $1 AND valid_to IS NULL
		ORDER BY valid_from DESC LIMIT 1`, issuer).
		Scan(&k.Kid, &k.Issuer, &k.PublicKey, &k.SecretKey, &k.ValidFrom, &k.ValidTo)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, core.ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return k, nil
}

// ListKeys 列出一个签发者的全部密钥（含已轮换掉的）。
func (s *Store) ListKeys(ctx context.Context, issuer string) ([]core.Key, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT kid, issuer, public_key, secret_key, valid_from, valid_to
		FROM issuer_keys WHERE issuer = $1 ORDER BY valid_from`, issuer)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []core.Key
	for rows.Next() {
		var k core.Key
		if err := rows.Scan(&k.Kid, &k.Issuer, &k.PublicKey, &k.SecretKey, &k.ValidFrom, &k.ValidTo); err != nil {
			return nil, err
		}
		out = append(out, k)
	}
	return out, rows.Err()
}

// InsertCredential 写入一张凭证。
func (s *Store) InsertCredential(ctx context.Context, c *core.Credential) error {
	return s.pool.QueryRow(ctx, `
		INSERT INTO credentials
			(id, issuer, subject, purpose, not_before, not_after, content_digest, kid, signature)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
		RETURNING issued_at`,
		c.ID, c.Issuer, c.Subject, c.Purpose, c.NotBefore, c.NotAfter,
		c.ContentDigest, c.Kid, c.Signature).
		Scan(&c.IssuedAt)
}

// GetCredential 按 id 读取凭证。
func (s *Store) GetCredential(ctx context.Context, id string) (*core.Credential, error) {
	c := &core.Credential{}
	err := s.pool.QueryRow(ctx, `
		SELECT id, issuer, subject, purpose, not_before, not_after,
		       content_digest, kid, signature, issued_at
		FROM credentials WHERE id = $1`, id).
		Scan(&c.ID, &c.Issuer, &c.Subject, &c.Purpose, &c.NotBefore, &c.NotAfter,
			&c.ContentDigest, &c.Kid, &c.Signature, &c.IssuedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, core.ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return c, nil
}

// InsertRevocation 追加一条撤销事件并返回其快照号与记录时间。
func (s *Store) InsertRevocation(ctx context.Context, credentialID, reason string) (*core.RevocationEvent, error) {
	e := &core.RevocationEvent{CredentialID: credentialID, Reason: reason}
	err := s.pool.QueryRow(ctx, `
		INSERT INTO revocation_events (credential_id, reason)
		VALUES ($1, $2)
		RETURNING seq, recorded_at`, credentialID, reason).
		Scan(&e.Seq, &e.RecordedAt)
	return e, err
}

// ListRevocations 按快照号顺序列出一凭证的全部撤销事件。
func (s *Store) ListRevocations(ctx context.Context, credentialID string) ([]core.RevocationEvent, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT seq, credential_id, reason, recorded_at
		FROM revocation_events WHERE credential_id = $1 ORDER BY seq`, credentialID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []core.RevocationEvent
	for rows.Next() {
		var e core.RevocationEvent
		if err := rows.Scan(&e.Seq, &e.CredentialID, &e.Reason, &e.RecordedAt); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// IsRevokedAt 重放到 at 时刻为止、且不晚于 maxSeq 快照的撤销事件，
// 报告该凭证在该历史时刻是否已撤销。
func (s *Store) IsRevokedAt(ctx context.Context, credentialID string, at time.Time, maxSeq int64) (bool, error) {
	var revoked bool
	err := s.pool.QueryRow(ctx, `
		SELECT EXISTS(
			SELECT 1 FROM revocation_events
			WHERE credential_id = $1 AND recorded_at <= $2 AND seq <= $3)`,
		credentialID, at, maxSeq).Scan(&revoked)
	return revoked, err
}

// Snapshot 返回当前全局快照号（无任何撤销时为 0）。
func (s *Store) Snapshot(ctx context.Context) (int64, error) {
	var snap int64
	err := s.pool.QueryRow(ctx,
		`SELECT COALESCE(MAX(seq), 0) FROM revocation_events`).Scan(&snap)
	return snap, err
}
