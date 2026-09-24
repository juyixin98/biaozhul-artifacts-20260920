package store

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
)

// PutArtifact 登记产物元数据。digest 已存在即幂等（要求路径一致）。
func (d *DB) PutArtifact(ctx context.Context, digest, storagePath string, size int64) error {
	_, err := d.Pool.Exec(ctx, `
		INSERT INTO artifacts(digest, size_bytes, storage_path)
		VALUES ($1,$2,$3)
		ON CONFLICT (digest) DO NOTHING`,
		digest, size, storagePath)
	return err
}

func (d *DB) GetArtifact(ctx context.Context, digest string) (*Artifact, error) {
	row := d.Pool.QueryRow(ctx, `
		SELECT digest, size_bytes, storage_path, uploaded_at
		FROM artifacts WHERE digest=$1`, digest)
	var a Artifact
	if err := row.Scan(&a.Digest, &a.SizeBytes, &a.StoragePath, &a.UploadedAt); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &a, nil
}

// PutEvidence 登记不可变证据版本。(artifact_digest, version) 冲突报 ErrAlreadyExists。
func (d *DB) PutEvidence(ctx context.Context, e EvidenceRow) error {
	_, err := d.Pool.Exec(ctx, `
		INSERT INTO test_evidence
		  (evidence_id, version, artifact_digest, tests_passed, result_json, signer_key_id, signature)
		VALUES ($1,$2,$3,$4,$5,$6,$7)`,
		e.EvidenceID, e.Version, e.ArtifactDigest, e.TestsPassed,
		e.ResultJSON, e.SignerKeyID, e.Signature)
	if err != nil {
		if isUniqueViolation(err) {
			return ErrAlreadyExists
		}
		return err
	}
	return nil
}

func (d *DB) GetEvidence(ctx context.Context, evidenceID string, version int64) (*EvidenceRow, error) {
	row := d.Pool.QueryRow(ctx, `
		SELECT evidence_id, version, artifact_digest, tests_passed, result_json,
		       signer_key_id, signature, created_at
		FROM test_evidence WHERE evidence_id=$1 AND version=$2`,
		evidenceID, version)
	var e EvidenceRow
	if err := row.Scan(&e.EvidenceID, &e.Version, &e.ArtifactDigest, &e.TestsPassed,
		&e.ResultJSON, &e.SignerKeyID, &e.Signature, &e.CreatedAt); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &e, nil
}

// PutPolicy 登记不可变策略版本
func (d *DB) PutPolicy(ctx context.Context, p PolicyRow) error {
	_, err := d.Pool.Exec(ctx, `
		INSERT INTO policies(policy_id, version, body_json, body_sha256, signer_key_id, signature)
		VALUES ($1,$2,$3,$4,$5,$6)`,
		p.PolicyID, p.Version, p.BodyJSON, p.BodySHA256, p.SignerKeyID, p.Signature)
	if err != nil {
		if isUniqueViolation(err) {
			return ErrAlreadyExists
		}
		return err
	}
	return nil
}

func (d *DB) GetPolicy(ctx context.Context, policyID string, version int64) (*PolicyRow, error) {
	row := d.Pool.QueryRow(ctx, `
		SELECT policy_id, version, body_json, body_sha256, signer_key_id, signature, created_at
		FROM policies WHERE policy_id=$1 AND version=$2`, policyID, version)
	var p PolicyRow
	if err := row.Scan(&p.PolicyID, &p.Version, &p.BodyJSON, &p.BodySHA256,
		&p.SignerKeyID, &p.Signature, &p.CreatedAt); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &p, nil
}

// ---- 浮动标签（仅检索用） ----

// UpsertTag 把标签（重新）指向某 digest；标签因此会“浮动”，晋升禁止使用
func (d *DB) UpsertTag(ctx context.Context, name, digest string) error {
	_, err := d.Pool.Exec(ctx, `
		INSERT INTO tags(name, digest, gen) VALUES ($1,$2,1)
		ON CONFLICT (name) DO UPDATE SET digest=EXCLUDED.digest, gen=tags.gen+1, updated_at=now()`,
		name, digest)
	return err
}

func (d *DB) GetTag(ctx context.Context, name string) (*TagRow, error) {
	row := d.Pool.QueryRow(ctx, `SELECT name, digest, gen, updated_at FROM tags WHERE name=$1`, name)
	var t TagRow
	if err := row.Scan(&t.Name, &t.Digest, &t.Gen, &t.UpdatedAt); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &t, nil
}
