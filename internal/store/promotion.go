package store

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
)

// ---- 审批 ----

func (d *DB) PutApproval(ctx context.Context, a ApprovalRow) error {
	_, err := d.Pool.Exec(ctx, `
		INSERT INTO approvals
		  (approval_id, policy_id, policy_version, env, artifact_digest,
		   evidence_id, evidence_version, expected_gen, signer_key_id, signature, status)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'valid')`,
		a.ApprovalID, a.PolicyID, a.PolicyVersion, a.Env, a.ArtifactDigest,
		a.EvidenceID, a.EvidenceVersion, a.ExpectedGen, a.SignerKeyID, a.Signature)
	if err != nil {
		if isUniqueViolation(err) {
			return ErrAlreadyExists
		}
		return err
	}
	return nil
}

// GetApproval 读取审批
func (d *DB) GetApproval(ctx context.Context, id string) (*ApprovalRow, error) {
	row := d.Pool.QueryRow(ctx, `
		SELECT approval_id, policy_id, policy_version, env, artifact_digest,
		       evidence_id, evidence_version, expected_gen, signer_key_id, signature,
		       status, consumed_by, created_at, consumed_at
		FROM approvals WHERE approval_id=$1`, id)
	return scanApproval(row)
}

// ConsumeApproval 在事务内把 valid 审批原子地标记为 consumed。
// 已消费/失效/不存在都返回错误——同一审批签名不能被两次晋升复用。
func ConsumeApproval(ctx context.Context, tx pgx.Tx, id, attemptID string) (*ApprovalRow, error) {
	row := tx.QueryRow(ctx, `
		UPDATE approvals
		SET status='consumed', consumed_by=$2, consumed_at=now()
		WHERE approval_id=$1 AND status='valid'
		RETURNING approval_id, policy_id, policy_version, env, artifact_digest,
		          evidence_id, evidence_version, expected_gen, signer_key_id, signature,
		          status, consumed_by, created_at, consumed_at`, id, attemptID)
	a, err := scanApproval(row)
	if errors.Is(err, pgx.ErrNoRows) {
		existing, gerr := func() (*ApprovalRow, error) {
			r := tx.QueryRow(ctx, `
				SELECT approval_id, policy_id, policy_version, env, artifact_digest,
				       evidence_id, evidence_version, expected_gen, signer_key_id, signature,
				       status, consumed_by, created_at, consumed_at
				FROM approvals WHERE approval_id=$1`, id)
			return scanApproval(r)
		}()
		if gerr == nil && existing.Status == "consumed" {
			return nil, ErrApprovalConsumed
		}
		return nil, ErrApprovalNotValid
	}
	return a, err
}

var (
	ErrApprovalConsumed = errors.New("approval already consumed")
	ErrApprovalNotValid = errors.New("approval not in valid state")
)

type rowScanner interface {
	Scan(dest ...any) error
}

func scanApproval(row rowScanner) (*ApprovalRow, error) {
	var a ApprovalRow
	if err := row.Scan(&a.ApprovalID, &a.PolicyID, &a.PolicyVersion, &a.Env,
		&a.ArtifactDigest, &a.EvidenceID, &a.EvidenceVersion, &a.ExpectedGen,
		&a.SignerKeyID, &a.Signature, &a.Status, &a.ConsumedBy, &a.CreatedAt,
		&a.ConsumedAt); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &a, nil
}

// ---- 晋升尝试：每次尝试一行完整证据，成功失败都保留 ----

func (d *DB) InsertAttempt(ctx context.Context, a AttemptRow) error {
	_, err := d.Pool.Exec(ctx, `
		INSERT INTO promotion_attempts
		  (id, env, idem_key, requested_digest, source_evidence_id, evidence_version,
		   policy_id, policy_version, expected_gen, approval_id, status,
		   failure_stage, failure_reason, copy_src_path, copy_dst_path,
		   copied_verified_digest, gen_before, gen_after, receipt_json, finished_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)`,
		a.ID, a.Env, a.IdemKey, a.RequestedDigest, a.SourceEvidenceID, a.EvidenceVersion,
		a.PolicyID, a.PolicyVersion, a.ExpectedGen, a.ApprovalID, a.Status,
		a.FailureStage, a.FailureReason, a.CopySrcPath, a.CopyDstPath,
		a.CopiedVerifiedDigest, a.GenBefore, a.GenAfter, a.ReceiptJSON, a.FinishedAt)
	if err != nil {
		if isUniqueViolation(err) {
			return ErrAlreadyExists
		}
	}
	return err
}

// GetAttemptByIdem 按幂等键取回之前那次尝试
func (d *DB) GetAttemptByIdem(ctx context.Context, env, key string) (*AttemptRow, error) {
	row := d.Pool.QueryRow(ctx, `SELECT `+attemptCols+`
		FROM promotion_attempts WHERE env=$1 AND idem_key=$2`, env, key)
	return scanAttempt(row)
}

func scanAttempt(row rowScanner) (*AttemptRow, error) {
	var a AttemptRow
	if err := row.Scan(&a.ID, &a.Env, &a.IdemKey, &a.RequestedDigest, &a.SourceEvidenceID,
		&a.EvidenceVersion, &a.PolicyID, &a.PolicyVersion, &a.ExpectedGen,
		&a.ApprovalID, &a.Status, &a.FailureStage, &a.FailureReason,
		&a.CopySrcPath, &a.CopyDstPath, &a.CopiedVerifiedDigest,
		&a.GenBefore, &a.GenAfter, &a.ReceiptJSON, &a.CreatedAt, &a.FinishedAt); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &a, nil
}

const attemptCols = `id, env, idem_key, requested_digest, source_evidence_id, evidence_version,
	policy_id, policy_version, expected_gen, approval_id, status,
	failure_stage, failure_reason, copy_src_path, copy_dst_path,
	copied_verified_digest, gen_before, gen_after, receipt_json, created_at, finished_at`

func (d *DB) GetAttempt(ctx context.Context, id string) (*AttemptRow, error) {
	row := d.Pool.QueryRow(ctx, `SELECT `+attemptCols+`
		FROM promotion_attempts WHERE id=$1`, id)
	return scanAttempt(row)
}

func (d *DB) ListAttempts(ctx context.Context, env string, limit int) ([]AttemptRow, error) {
	rows, err := d.Pool.Query(ctx, `SELECT `+attemptCols+`
		FROM promotion_attempts WHERE env=$1
		ORDER BY created_at DESC, id DESC LIMIT $2`, env, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return collectAttempts(rows)
}

func (d *DB) ListUnfinishedAttempts(ctx context.Context) ([]AttemptRow, error) {
	rows, err := d.Pool.Query(ctx, `SELECT `+attemptCols+`
		FROM promotion_attempts WHERE status IN ('in_progress','awaiting_approval')
		ORDER BY created_at`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return collectAttempts(rows)
}

func collectAttempts(rows pgx.Rows) ([]AttemptRow, error) {
	var out []AttemptRow
	for rows.Next() {
		a, err := scanAttempt(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *a)
	}
	return out, rows.Err()
}

// FinishAttemptOutsideTx 在复制失败/拒绝等未开事务的路径上终结尝试
func (d *DB) FinishAttemptOutsideTx(ctx context.Context, a AttemptRow) error {
	_, err := d.Pool.Exec(ctx, finishAttemptSQL, finishArgs(a)...)
	return err
}

// FinishAttemptTx 在事务内终结尝试
func FinishAttemptTx(ctx context.Context, tx pgx.Tx, a AttemptRow) error {
	_, err := tx.Exec(ctx, finishAttemptSQL, finishArgs(a)...)
	return err
}

const finishAttemptSQL = `
	UPDATE promotion_attempts SET
	  status=$2, failure_stage=$3, failure_reason=$4,
	  copy_src_path=$5, copy_dst_path=$6, copied_verified_digest=$7,
	  gen_before=$8, gen_after=$9, receipt_json=$10, finished_at=now()
	WHERE id=$1`

func finishArgs(a AttemptRow) []any {
	return []any{
		a.ID, a.Status, a.FailureStage, a.FailureReason,
		a.CopySrcPath, a.CopyDstPath, a.CopiedVerifiedDigest,
		a.GenBefore, a.GenAfter, a.ReceiptJSON,
	}
}

// MarkRecovered 启动恢复时给中断尝试打恢复结论
func (d *DB) MarkRecovered(ctx context.Context, id, status, reason string) error {
	_, err := d.Pool.Exec(ctx, `
		UPDATE promotion_attempts
		SET status=$2, failure_stage='recovery', failure_reason=$3, finished_at=now()
		WHERE id=$1`, id, status, reason)
	return err
}
