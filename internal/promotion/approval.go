package promotion

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"atomicpromo/internal/approval"
	"atomicpromo/internal/blob"
	"atomicpromo/internal/store"
)

// RegisterApproval 登记一份审批决策（只验签存储，不改变任何环境状态）。
// 审批可以早于晋升请求到达（“早到”），由随后的晋升请求消费。
func (s *Service) RegisterApproval(ctx context.Context, d approval.Decision) error {
	if err := d.Verify(); err != nil {
		return fmt.Errorf("%w: %v", ErrApprovalReject, err)
	}
	if !validEnv(d.Env) || !validDigest(d.ArtifactDigest) {
		return fmt.Errorf("%w: approval must pin env and immutable digest", ErrApprovalReject)
	}
	row := store.ApprovalRow{
		ApprovalID:      d.ApprovalID,
		PolicyID:        d.PolicyID,
		PolicyVersion:   d.PolicyVersion,
		Env:             d.Env,
		ArtifactDigest:  d.ArtifactDigest,
		EvidenceID:      d.EvidenceID,
		EvidenceVersion: d.EvidenceVersion,
		ExpectedGen:     d.ExpectedGen,
		SignerKeyID:     d.SignerKeyID,
		Signature:       d.Signature,
	}
	if err := s.db.PutApproval(ctx, row); err != nil {
		return fmt.Errorf("register approval: %w", err)
	}
	return nil
}

// checkApprovalUsable 校验审批签名 + 审批元组与本次晋升逐项匹配 + 代次（迟到检测）。
// consume 不在此处发生——消费只能发生在指针切换事务里。
func (s *Service) checkApprovalUsable(ctx context.Context, approvalID string,
	req PromoteRequest, attemptID string) string {

	a, err := s.db.GetApproval(ctx, approvalID)
	if err != nil {
		return "approval not found: " + err.Error()
	}
	if a.Status != "valid" {
		return fmt.Sprintf("approval %s is %s (each approval can be consumed at most once)", approvalID, a.Status)
	}
	d := approval.Decision{
		ApprovalID:      a.ApprovalID,
		PolicyID:        a.PolicyID,
		PolicyVersion:   a.PolicyVersion,
		Env:             a.Env,
		ArtifactDigest:  a.ArtifactDigest,
		EvidenceID:      a.EvidenceID,
		EvidenceVersion: a.EvidenceVersion,
		ExpectedGen:     a.ExpectedGen,
		SignerKeyID:     a.SignerKeyID,
		Signature:       a.Signature,
	}
	if err := d.Verify(); err != nil {
		return "approval signature invalid: " + err.Error()
	}
	t := d.Tuple()
	if t.Env != req.Env || t.ArtifactDigest != req.Digest ||
		t.EvidenceID != req.EvidenceID || t.EvidenceVersion != req.EvidenceVer ||
		t.PolicyID != req.PolicyID || t.PolicyVersion != req.PolicyVer {
		return "approval tuple does not match the promotion request"
	}
	if t.ExpectedGen != req.ExpectedGen {
		return fmt.Sprintf("approval expected_gen %d != request expected_gen %d", t.ExpectedGen, req.ExpectedGen)
	}
	return ""
}

func aExpectedGen(a *store.ApprovalRow) int64 { return a.ExpectedGen }

func aExpectedGenSafe(a *store.ApprovalRow) int64 { return a.ExpectedGen }

func strEq(p *string, s string) bool { return p != nil && *p == s }

func int64Eq(p *int64, v int64) bool { return p != nil && *p == v }

// CompleteApprovedPromotion 用已登记的审批完成一次挂起的晋级（两阶段路径）。
func (s *Service) CompleteApprovedPromotion(ctx context.Context, approvalID, idemKey string) (Outcome, error) {
	ap, err := s.db.GetApproval(ctx, approvalID)
	if err != nil {
		return Outcome{}, fmt.Errorf("%w: %v", ErrApprovalReject, err)
	}
	if ap.Status == "consumed" {
		// 幂等：返回该审批实际完成的那次尝试
		if ap.ConsumedBy != nil {
			a, gerr := s.db.GetAttempt(ctx, *ap.ConsumedBy)
			if gerr == nil {
				return s.attemptToOutcome(ctx, a)
			}
		}
		return Outcome{}, fmt.Errorf("%w: approval already consumed", ErrApprovalReject)
	}
	if ap.Status != "valid" {
		return Outcome{}, fmt.Errorf("%w: approval status %s", ErrApprovalReject, ap.Status)
	}

	// 重放签名校验（防止存储内容被替换）
	d := approval.Decision{
		ApprovalID: ap.ApprovalID, PolicyID: ap.PolicyID, PolicyVersion: ap.PolicyVersion,
		Env: ap.Env, ArtifactDigest: ap.ArtifactDigest, EvidenceID: ap.EvidenceID,
		EvidenceVersion: ap.EvidenceVersion, ExpectedGen: ap.ExpectedGen,
		SignerKeyID: ap.SignerKeyID, Signature: ap.Signature,
	}
	if err := d.Verify(); err != nil {
		return Outcome{}, fmt.Errorf("%w: %v", ErrApprovalReject, err)
	}

	ptr, err := s.db.GetPointer(ctx, ap.Env)
	if err != nil {
		// 新环境以 gen 0 处理
		ptr = &store.PointerRow{Env: ap.Env, Gen: 0}
		if !errors.Is(err, store.ErrNotFound) {
			return Outcome{}, err
		}
	}

	req := PromoteRequest{
		Env: ap.Env, Digest: ap.ArtifactDigest,
		EvidenceID: ap.EvidenceID, EvidenceVer: ap.EvidenceVersion,
		PolicyID: ap.PolicyID, PolicyVer: ap.PolicyVersion,
		ExpectedGen: ptr.Gen,
	}
	if idemKey != "" {
		req.IdempotencyKey = &idemKey
	}

	// 迟到审批：审批钉的代次 != 当前代次 → 拒绝，但审批记录原样保留为审计证据
	if ap.ExpectedGen != ptr.Gen {
		return s.lateApprovalOutcome(ctx, req, ap,
			fmt.Sprintf("late approval: approval pins gen %d, environment now at gen %d",
				ap.ExpectedGen, ptr.Gen))
	}

	v, reason, verr := s.validate(ctx, req)
	if verr != nil {
		return s.lateApprovalOutcome(ctx, req, ap, "revalidation failed: "+reason)
	}
	if !v.pol.ApprovalRequired {
		// 策略版本本身不要求审批：允许消费但按普通晋升校验，保持语义一致
	}

	var a store.AttemptRow
	var existing *store.AttemptRow
	if pending := s.findPendingAttempt(ctx, ap); pending != nil {
		existing = pending
		a = *pending
		a.ApprovalID = &ap.ApprovalID
	} else {
		a = s.newAttemptRow(req, "in_progress")
		a.ApprovalID = &ap.ApprovalID
		if err := s.db.InsertAttempt(ctx, a); err != nil {
			return Outcome{}, err
		}
	}

	// 复制（若挂起阶段已复制完成则幂等复用，重新校验）
	verified, copyErr := s.blobs.CopyToEnv(ctx, req.Env, req.Digest)
	if copyErr != nil {
		if existing != nil {
			// 挂起尝试直接更新
			a2 := a
			a2.CopiedVerifiedDigest = nil
			return s.finishExisting(ctx, a2, "copy_failed", StageCopy,
				"artifact copy or post-copy verification failed: "+copyErr.Error()), nil
		}
		return s.errOutcome(ctx, a, "copy_failed", StageCopy,
			"artifact copy or post-copy verification failed: "+copyErr.Error()), nil
	}
	a.CopiedVerifiedDigest = &verified

	out, cerr := s.commitPromotion(ctx, a, req, v, &ap.ApprovalID)
	_ = blob.CanonicalPath // keep import if unused in this file
	return out, cerr
}

func (s *Service) findPendingAttempt(ctx context.Context, ap *store.ApprovalRow) *store.AttemptRow {
	rows, err := s.db.ListAttempts(ctx, ap.Env, 50)
	if err != nil {
		return nil
	}
	for i := range rows {
		a := rows[i]
		if a.Status == "awaiting_approval" &&
			a.RequestedDigest == ap.ArtifactDigest &&
			a.ExpectedGen == aExpectedGenSafe(ap) &&
			strEq(a.SourceEvidenceID, ap.EvidenceID) &&
			int64Eq(a.EvidenceVersion, ap.EvidenceVersion) &&
			strEq(a.PolicyID, ap.PolicyID) &&
			int64Eq(a.PolicyVersion, ap.PolicyVersion) {
			return &a
		}
	}
	return nil
}

// lateApprovalOutcome 把迟到/失效审批落成一条 conflict 尝试证据（不消费审批）
func (s *Service) lateApprovalOutcome(ctx context.Context, req PromoteRequest,
	ap *store.ApprovalRow, reason string) (Outcome, error) {
	a := s.newAttemptRow(req, "in_progress")
	a.ApprovalID = &ap.ApprovalID
	if err := s.db.InsertAttempt(ctx, a); err != nil {
		return Outcome{}, err
	}
	_ = json.RawMessage(nil)
	return s.errOutcome(ctx, a, "conflict", StageValidate, reason), nil
}

func (s *Service) finishExisting(ctx context.Context, a store.AttemptRow,
	status string, stage Stage, reason string) Outcome {
	return s.errOutcome(ctx, a, status, stage, reason)
}
