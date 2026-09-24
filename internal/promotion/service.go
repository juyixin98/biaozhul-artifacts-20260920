// Package promotion 编排“测试 → 预发布”的原子晋级：
//
//  1. 只接受不可变引用（artifact digest / evidence version / policy version），
//     重新独立验签证据与策略，任何浮动引用一律拒绝；
//  2. 真实字节复制到目标环境并在切换前重新哈希校验；
//  3. 单事务 + 环境代次 CAS 切换指针，并发晋级/回退必有一方得到 conflict；
//  4. 复制失败、校验失败、事务失败都不触碰旧指针（中断保留旧可用版本）；
//  5. 每次尝试（含失败）都把完整证据写入 promotion_attempts。
package promotion

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"strings"

	"github.com/google/uuid"

	"atomicpromo/internal/blob"
	"atomicpromo/internal/crypto/sig"
	"atomicpromo/internal/evidence"
	"atomicpromo/internal/policy"
	"atomicpromo/internal/receipt"
	"atomicpromo/internal/store"
)

var envNameRe = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// SentinelError 语义化错误，HTTP 层据此映射状态码
var (
	ErrBadRequest      = errors.New("bad request")
	ErrImmutableRef    = fmt.Errorf("%w: only immutable sha256 digest references are accepted", ErrBadRequest)
	ErrEvidenceReject  = errors.New("evidence rejected")
	ErrPolicyReject    = errors.New("policy rejected")
	ErrApprovalReject  = errors.New("approval rejected")
	ErrRetentionReject = errors.New("retention rule not satisfied")
	ErrRollbackTarget  = errors.New("rollback target invalid")
)

type Service struct {
	db     *store.DB
	blobs  *blob.Store
	signer *receipt.Signer
	log    *slog.Logger
}

func New(db *store.DB, blobs *blob.Store, signer *receipt.Signer, log *slog.Logger) *Service {
	return &Service{db: db, blobs: blobs, signer: signer, log: log}
}

func validEnv(env string) bool { return envNameRe.MatchString(env) }

func validDigest(d string) bool {
	if !strings.HasPrefix(d, sig.DigestPrefix) {
		return false
	}
	hx := strings.TrimPrefix(d, sig.DigestPrefix)
	if len(hx) != 64 {
		return false
	}
	for _, c := range hx {
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return false
		}
	}
	return true
}

type validated struct {
	ev     *store.EvidenceRow
	evBody evidence.Envelope
	po     *store.PolicyRow
	pol    policy.Body
}

// validate 重新独立核验所有不可变引用（不信任调用方任何转述）
func (s *Service) validate(ctx context.Context, req PromoteRequest) (*validated, string, error) {
	if !validEnv(req.Env) {
		return nil, "invalid env name", ErrBadRequest
	}
	if req.ExpectedGen < 0 {
		return nil, "expected_gen must be >= 0", ErrBadRequest
	}
	if !validDigest(req.Digest) {
		return nil, "digest must be an immutable sha256:<hex> reference; tags/floating refs are forbidden", ErrImmutableRef
	}
	if req.EvidenceID == "" || req.EvidenceVer <= 0 || req.PolicyID == "" || req.PolicyVer <= 0 {
		return nil, "evidence_id/evidence_version/policy_id/policy_version are required (immutable versions)", ErrBadRequest
	}

	if _, err := s.db.GetArtifact(ctx, req.Digest); err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return nil, "artifact with that digest is not registered", ErrImmutableRef
		}
		return nil, err.Error(), err
	}

	ev, err := s.db.GetEvidence(ctx, req.EvidenceID, req.EvidenceVer)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return nil, "evidence version not found", ErrEvidenceReject
		}
		return nil, err.Error(), err
	}
	var evBody evidence.Envelope
	if err := json.Unmarshal([]byte(ev.ResultJSON), &evBody.Result); err != nil {
		return nil, err.Error(), err
	}
	evBody.EvidenceID, evBody.Version = ev.EvidenceID, ev.Version
	evBody.ArtifactDigest, evBody.TestsPassed = ev.ArtifactDigest, ev.TestsPassed
	evBody.SignerKeyID, evBody.Signature = ev.SignerKeyID, ev.Signature
	if err := evBody.Verify(); err != nil { // 重新 Ed25519 验签
		return nil, "evidence signature invalid: " + err.Error(), ErrEvidenceReject
	}
	if ev.ArtifactDigest != req.Digest {
		return nil, "evidence is bound to a different artifact digest", ErrEvidenceReject
	}
	if !ev.TestsPassed {
		return nil, "evidence says tests did not pass", ErrEvidenceReject
	}

	po, err := s.db.GetPolicy(ctx, req.PolicyID, req.PolicyVer)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return nil, "policy version not found", ErrPolicyReject
		}
		return nil, err.Error(), err
	}
	pol, err := policy.UnmarshalBody(po.BodyJSON)
	if err != nil {
		return nil, err.Error(), err
	}
	wantHash, err := pol.VersionHash()
	if err != nil {
		return nil, err.Error(), err
	}
	if wantHash != po.BodySHA256 {
		return nil, "stored policy body hash mismatch (tampered)", ErrPolicyReject
	}
	signed := policy.SignedBody{Body: pol, SignerKeyID: po.SignerKeyID, Signature: po.Signature}
	if err := signed.Verify(); err != nil { // 重新 Ed25519 验签
		return nil, "policy signature invalid: " + err.Error(), ErrPolicyReject
	}

	if pol.MinSigners > 0 && evBody.SignerKeyID == "" {
		return nil, "policy requires signers but evidence has none", ErrPolicyReject
	}
	for _, t := range pol.RequiredTests {
		if !evidence.RequiredTestPassed(evBody.Result, t) {
			return nil, fmt.Sprintf("required test %q did not pass in evidence v%d", t, ev.Version), ErrPolicyReject
		}
	}
	return &validated{ev: ev, evBody: evBody, po: po, pol: pol}, "", nil
}

// errOutcome 把失败尝试落库（每次尝试都保留完整证据）
func (s *Service) errOutcome(ctx context.Context, a store.AttemptRow, status string, stage Stage, reason string) Outcome {
	a.Status = status
	rs := string(stage)
	a.FailureStage = &rs
	a.FailureReason = &reason
	if err := s.db.FinishAttemptOutsideTx(ctx, a); err != nil {
		s.log.Error("finish failed attempt", "attempt", a.ID, "err", err)
	}
	return Outcome{AttemptID: a.ID, Status: status, Env: a.Env, Reason: reason}
}

func newUUID() string { return uuid.NewString() }
