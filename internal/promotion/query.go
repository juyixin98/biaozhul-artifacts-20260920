package promotion

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"

	"atomicpromo/internal/blob"
	"atomicpromo/internal/evidence"
	"atomicpromo/internal/policy"
	"atomicpromo/internal/store"
)

// UploadArtifact 流式落盘内容寻址仓库，并以重新计算的真实 digest 落账。
// 调用方传入的 expectedDigest 若非空则必须与实算一致（防止上传者冒名）。
func (s *Service) UploadArtifact(ctx context.Context, r io.Reader, expectedDigest string) (string, int64, error) {
	digest, size, err := s.blobs.PutCanonical(r)
	if err != nil {
		return "", 0, err
	}
	if expectedDigest != "" && expectedDigest != digest {
		return "", 0, fmt.Errorf("%w: content hash %s does not match declared digest %s",
			ErrImmutableRef, digest, expectedDigest)
	}
	rel, err := blob.CanonicalPath(digest)
	if err != nil {
		return "", 0, err
	}
	if err := s.db.PutArtifact(ctx, digest, rel, size); err != nil {
		return "", 0, err
	}
	return digest, size, nil
}

// RegisterEvidence 登记一份测试证据：先重新验签，再按不可变版本落库
func (s *Service) RegisterEvidence(ctx context.Context, e evidence.Envelope) error {
	if err := e.Verify(); err != nil {
		return fmt.Errorf("%w: %v", ErrEvidenceReject, err)
	}
	if _, err := s.db.GetArtifact(ctx, e.ArtifactDigest); err != nil {
		return fmt.Errorf("%w: artifact must be uploaded before evidence", ErrImmutableRef)
	}
	raw, err := json.Marshal(e.Result)
	if err != nil {
		return err
	}
	row := store.EvidenceRow{
		EvidenceID: e.EvidenceID, Version: e.Version, ArtifactDigest: e.ArtifactDigest,
		TestsPassed: e.TestsPassed, ResultJSON: string(raw),
		SignerKeyID: e.SignerKeyID, Signature: e.Signature,
	}
	if err := s.db.PutEvidence(ctx, row); err != nil {
		if errors.Is(err, store.ErrAlreadyExists) {
			return fmt.Errorf("evidence version already exists (versions are immutable): %w", err)
		}
		return err
	}
	return nil
}

// RegisterPolicy 登记一个不可变策略版本（重新验签 + 计算版本哈希）
func (s *Service) RegisterPolicy(ctx context.Context, sb policy.SignedBody) (string, error) {
	if err := sb.Verify(); err != nil {
		return "", fmt.Errorf("%w: %v", ErrPolicyReject, err)
	}
	raw, err := json.Marshal(sb.Body)
	if err != nil {
		return "", err
	}
	hash, err := sb.Body.VersionHash()
	if err != nil {
		return "", err
	}
	err = s.db.PutPolicy(ctx, store.PolicyRow{
		PolicyID: sb.Body.PolicyID, Version: sb.Body.Version,
		BodyJSON: string(raw), BodySHA256: hash,
		SignerKeyID: sb.SignerKeyID, Signature: sb.Signature,
	})
	if err != nil {
		if errors.Is(err, store.ErrAlreadyExists) {
			return "", fmt.Errorf("policy version already exists (create a new version instead): %w", err)
		}
		return "", err
	}
	return hash, nil
}

// MoveTag 把浮动标签指向某 digest（仅检索；晋升接口拒绝标签）
func (s *Service) MoveTag(ctx context.Context, name, digest string) error {
	if !validDigest(digest) {
		return fmt.Errorf("%w: tag target must be sha256 digest", ErrImmutableRef)
	}
	if _, err := s.db.GetArtifact(ctx, digest); err != nil {
		return fmt.Errorf("artifact not registered: %w", err)
	}
	return s.db.UpsertTag(ctx, name, digest)
}

func (s *Service) GetTag(ctx context.Context, name string) (*store.TagRow, error) {
	return s.db.GetTag(ctx, name)
}

// ---- 读模型 ----

type EnvView struct {
	Env           string             `json:"env"`
	Gen           int64              `json:"gen"`
	CurrentDigest *string            `json:"current_digest"`
	History       []store.HistoryRow `json:"history"`
}

func (s *Service) GetEnvironment(ctx context.Context, env string) (*EnvView, error) {
	if !validEnv(env) {
		return nil, fmt.Errorf("%w: invalid env", ErrBadRequest)
	}
	ptr, err := s.db.GetPointer(ctx, env)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return &EnvView{Env: env, Gen: 0, History: []store.HistoryRow{}}, nil
		}
		return nil, err
	}
	hist, err := s.db.ListHistory(ctx, env)
	if err != nil {
		return nil, err
	}
	return &EnvView{Env: env, Gen: ptr.Gen, CurrentDigest: ptr.CurrentDigest, History: hist}, nil
}

func (s *Service) GetAttempt(ctx context.Context, id string) (*store.AttemptRow, error) {
	return s.db.GetAttempt(ctx, id)
}

func (s *Service) ListAttempts(ctx context.Context, env string, limit int) ([]store.AttemptRow, error) {
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	return s.db.ListAttempts(ctx, env, limit)
}
