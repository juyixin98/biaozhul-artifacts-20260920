package store

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
)

// UpsertEnvBlob 登记“已复制且已校验”的环境副本（指针切换前必须先有这行）
func UpsertEnvBlob(ctx context.Context, tx pgx.Tx, b EnvBlob) error {
	_, err := tx.Exec(ctx, `
		INSERT INTO env_blobs(env, digest, verified_digest)
		VALUES ($1,$2,$3)
		ON CONFLICT (env, digest) DO UPDATE SET verified_digest=EXCLUDED.verified_digest`,
		b.Env, b.Digest, b.VerifiedDigest)
	return err
}

// EnvBlobExistsTx 事务内确认环境副本已存在且校验摘要一致
func EnvBlobExistsTx(ctx context.Context, tx pgx.Tx, env, digest string) (bool, error) {
	var ok bool
	err := tx.QueryRow(ctx,
		`SELECT EXISTS(SELECT 1 FROM env_blobs WHERE env=$1 AND digest=$2 AND verified_digest=$2)`,
		env, digest).Scan(&ok)
	return ok, err
}

// EnsurePointer 为新环境插入空指针（gen=0, current=NULL）
func (d *DB) EnsurePointer(ctx context.Context, env string) error {
	_, err := d.Pool.Exec(ctx,
		`INSERT INTO env_pointers(env) VALUES ($1) ON CONFLICT (env) DO NOTHING`, env)
	return err
}

// GetPointer 读当前指针
func (d *DB) GetPointer(ctx context.Context, env string) (*PointerRow, error) {
	row := d.Pool.QueryRow(ctx,
		`SELECT env, current_digest, gen, updated_at FROM env_pointers WHERE env=$1`, env)
	var p PointerRow
	if err := row.Scan(&p.Env, &p.CurrentDigest, &p.Gen, &p.UpdatedAt); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &p, nil
}

// PromotionCommit 是整个系统的原子核心：在单事务内
//  1. 锁定环境指针行；
//  2. 用 expected_gen 做乐观并发校验（代次不符 → ErrConflict，绝不覆盖）；
//  3. 确认新 digest 的环境副本已经复制并校验落账；
//  4. （可选）原子消费审批；
//  5. 先写 env_history(gen+1)，再切换 current_digest、gen+1；
//  6. 尝试记录置 committed。
//
// 任一步失败整体回滚：副本可能已复制（下次幂等复用），但指针永远停在旧版本。
type CommitInput struct {
	Env                  string
	NewDigest            string
	CopiedVerifiedDigest string // 复制后重新哈希结果；与指针提交同事务落库
	ExpectedGen          int64
	ChangeType           string // promote | rollback
	AttemptID            string
	PolicyID             *string
	PolicyVersion        *int64
	EvidenceID           *string
	EvidenceVersion      *int64
	ApprovalID           *string
	ReceiptJSON          *string
}

func (d *DB) PromotionCommit(ctx context.Context, in CommitInput) (genBefore, genAfter int64, err error) {
	tx, err := d.Pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return 0, 0, err
	}
	defer tx.Rollback(ctx)

	// 1+2: 环境级咨询锁串行化同一环境的指针切换（新环境无行可锁，咨询锁对两者都成立）
	var lockID int64
	if err := tx.QueryRow(ctx, `SELECT hashtextextended($1, 0)`, in.Env).Scan(&lockID); err != nil {
		return 0, 0, err
	}
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, lockID); err != nil {
		return 0, 0, err
	}

	if _, err := tx.Exec(ctx,
		`INSERT INTO env_pointers(env) VALUES ($1) ON CONFLICT (env) DO NOTHING`, in.Env); err != nil {
		return 0, 0, err
	}

	var currentDigest *string
	if err := tx.QueryRow(ctx,
		`SELECT current_digest, gen FROM env_pointers WHERE env=$1`,
		in.Env).Scan(&currentDigest, &genBefore); err != nil {
		return 0, 0, err
	}
	if genBefore != in.ExpectedGen {
		return genBefore, genBefore, ErrConflict
	}

	// 3: 环境副本账。
	//    promote：物理复制与切换前重新哈希已在事务外完成，这里幂等登记；
	//    rollback：只允许指向仍然完整（未被 GC）的历史副本，锁内再次确认，
	//              绝不把一个已被回收的副本账“复活”。
	if in.ChangeType == "rollback" {
		ok, err := EnvBlobExistsTx(ctx, tx, in.Env, in.NewDigest)
		if err != nil {
			return genBefore, genBefore, err
		}
		if !ok {
			return genBefore, genBefore, errors.New("rollback target env blob is gone (outside retention set)")
		}
	} else {
		if err := UpsertEnvBlob(ctx, tx, EnvBlob{
			Env: in.Env, Digest: in.NewDigest, VerifiedDigest: in.NewDigest,
		}); err != nil {
			return genBefore, genBefore, err
		}
	}

	// 4: 审批原子消费
	if in.ApprovalID != nil {
		if _, err := ConsumeApproval(ctx, tx, *in.ApprovalID, in.AttemptID); err != nil {
			return genBefore, genBefore, err
		}
	}

	genAfter = genBefore + 1

	// 5: 历史先落账
	if _, err := tx.Exec(ctx, `
		INSERT INTO env_history
		  (env, gen, digest, change_type, attempt_id, policy_id, policy_version, evidence_id, evidence_version)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
		in.Env, genAfter, in.NewDigest, in.ChangeType, nullable(in.AttemptID),
		in.PolicyID, in.PolicyVersion, in.EvidenceID, in.EvidenceVersion); err != nil {
		return genBefore, genBefore, err
	}

	// 6: 切换指针
	if _, err := tx.Exec(ctx, `
		UPDATE env_pointers
		SET current_digest=$2, gen=$3, updated_at=now()
		WHERE env=$1 AND gen=$4`,
		in.Env, in.NewDigest, genAfter, genBefore); err != nil {
		return genBefore, genBefore, err
	}

	// 7: 尝试证据收尾（同事务，保证“提交成功”的尝试必有收据与复制校验摘要）
	if _, err := tx.Exec(ctx, `
		UPDATE promotion_attempts
		SET status='committed',
		    gen_before=$2, gen_after=$3, receipt_json=$4,
		    copied_verified_digest=COALESCE($5, copied_verified_digest),
		    finished_at=now()
		WHERE id=$1`, in.AttemptID, genBefore, genAfter, in.ReceiptJSON,
		nullableStr(in.CopiedVerifiedDigest)); err != nil {
		return genBefore, genBefore, err
	}

	if err := tx.Commit(ctx); err != nil {
		return genBefore, genBefore, err
	}
	return genBefore, genAfter, nil
}

// ListHistory 环境代次历史（旧 → 新）
func (d *DB) ListHistory(ctx context.Context, env string) ([]HistoryRow, error) {
	rows, err := d.Pool.Query(ctx, `
		SELECT env, gen, digest, change_type, attempt_id, policy_id, policy_version,
		       evidence_id, evidence_version, created_at
		FROM env_history WHERE env=$1 ORDER BY gen`, env)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []HistoryRow
	for rows.Next() {
		var h HistoryRow
		if err := rows.Scan(&h.Env, &h.Gen, &h.Digest, &h.ChangeType, &h.AttemptID,
			&h.PolicyID, &h.PolicyVersion, &h.EvidenceID, &h.EvidenceVersion, &h.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, h)
	}
	return out, rows.Err()
}

// EnvBlobDigests 该环境当前所有已复制的 digest（GC 用）
func (d *DB) EnvBlobDigests(ctx context.Context, env string) ([]string, error) {
	return envBlobDigests(ctx, d.Pool, env)
}

func (d *DB) EnvBlobDigestsTx(ctx context.Context, tx pgx.Tx, env string) ([]string, error) {
	return envBlobDigests(ctx, tx, env)
}

func envBlobDigests(ctx context.Context, q interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
}, env string) ([]string, error) {
	rows, err := q.Query(ctx,
		`SELECT digest FROM env_blobs WHERE env=$1 ORDER BY digest`, env)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var s string
		if err := rows.Scan(&s); err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// ListHistoryTx 事务内读历史（GC 锁内重读用）
func (d *DB) ListHistoryTx(ctx context.Context, tx pgx.Tx, env string) ([]HistoryRow, error) {
	rows, err := tx.Query(ctx, `
		SELECT env, gen, digest, change_type, attempt_id, policy_id, policy_version,
		       evidence_id, evidence_version, created_at
		FROM env_history WHERE env=$1 ORDER BY gen`, env)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []HistoryRow
	for rows.Next() {
		var h HistoryRow
		if err := rows.Scan(&h.Env, &h.Gen, &h.Digest, &h.ChangeType, &h.AttemptID,
			&h.PolicyID, &h.PolicyVersion, &h.EvidenceID, &h.EvidenceVersion, &h.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, h)
	}
	return out, rows.Err()
}

// DeleteEnvBlob 事务内删除环境副本账
func DeleteEnvBlob(ctx context.Context, tx pgx.Tx, env, digest string) error {
	_, err := tx.Exec(ctx, `DELETE FROM env_blobs WHERE env=$1 AND digest=$2`, env, digest)
	return err
}

// InsertGCRun 记录一次 GC 的完整依据与结果
func (d *DB) InsertGCRun(ctx context.Context, r GCRunRow) (int64, error) {
	row := d.Pool.QueryRow(ctx, `
		INSERT INTO gc_runs(policy_id, policy_version, keep_digests, removed_blobs, skipped_inuse)
		VALUES ($1,$2,$3,$4,$5) RETURNING id`,
		r.PolicyID, r.PolicyVersion, r.KeepDigests, r.RemovedBlobs, r.SkippedInuse)
	var id int64
	return id, row.Scan(&id)
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func nullableStr(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}
