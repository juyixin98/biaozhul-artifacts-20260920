package promotion

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"atomicpromo/internal/blob"
	"atomicpromo/internal/receipt"
	"atomicpromo/internal/store"
)

// Promote 执行一次晋级。返回的 Outcome 记录尝试 ID 与结果。
func (s *Service) Promote(ctx context.Context, req PromoteRequest) (Outcome, error) {
	// 幂等：同 (env, idempotency_key) 的重试直接取回首次尝试
	if req.IdempotencyKey != nil && *req.IdempotencyKey != "" {
		if got, err := s.db.GetAttemptByIdem(ctx, req.Env, *req.IdempotencyKey); err == nil {
			return s.attemptToOutcome(ctx, got)
		} else if !errors.Is(err, store.ErrNotFound) {
			return Outcome{}, err
		}
	}

	v, reason, verr := s.validate(ctx, req)
	if verr != nil {
		// 形状合法的请求也留下尝试证据（拒绝可审计）；明显坏请求在 HTTP 层 400，不进这里
		a := s.newAttemptRow(req, "in_progress")
		status := "rejected"
		switch {
		case errors.Is(verr, ErrEvidenceReject):
			status = "rejected"
		case errors.Is(verr, ErrPolicyReject):
			status = "rejected"
		case errors.Is(verr, ErrImmutableRef):
			status = "rejected"
		}
		if inerr := s.db.InsertAttempt(ctx, a); inerr != nil {
			if errors.Is(inerr, store.ErrAlreadyExists) { // 与幂等键竞争
				got, _ := s.db.GetAttemptByIdem(ctx, req.Env, *req.IdempotencyKey)
				if got != nil {
					return s.attemptToOutcome(ctx, got)
				}
			}
			return Outcome{}, inerr
		}
		return s.errOutcome(ctx, a, status, StageValidate, reason), nil
	}

	// 需要审批但本次没带审批：尝试置 awaiting_approval，等审批到达后走 CompleteApprovedPromotion
	if v.pol.ApprovalRequired && req.ApprovalID == nil {
		a := s.newAttemptRow(req, "awaiting_approval")
		if err := s.db.InsertAttempt(ctx, a); err != nil {
			if errors.Is(err, store.ErrAlreadyExists) {
				got, _ := s.db.GetAttemptByIdem(ctx, req.Env, *req.IdempotencyKey)
				if got != nil {
					return s.attemptToOutcome(ctx, got)
				}
			}
			return Outcome{}, err
		}
		return Outcome{AttemptID: a.ID, Status: "awaiting_approval", Env: req.Env,
			Reason: "policy requires approval; register an approval and POST /promotions/approve"}, nil
	}

	a := s.newAttemptRow(req, "in_progress")
	if req.ApprovalID != nil {
		a.ApprovalID = req.ApprovalID
	}
	if err := s.db.InsertAttempt(ctx, a); err != nil {
		if errors.Is(err, store.ErrAlreadyExists) && req.IdempotencyKey != nil {
			got, _ := s.db.GetAttemptByIdem(ctx, req.Env, *req.IdempotencyKey)
			if got != nil {
				return s.attemptToOutcome(ctx, got)
			}
		}
		return Outcome{}, err
	}

	if req.ApprovalID != nil {
		if rerr := s.checkApprovalUsable(ctx, *req.ApprovalID, req, a.ID); rerr != "" {
			return s.errOutcome(ctx, a, "rejected", StageValidate, rerr), nil
		}
	}

	// 真实复制 + 复制后重新哈希校验；失败绝不切换指针
	srcRel, _ := blob.CanonicalPath(req.Digest)
	ac := srcRel
	a.CopySrcPath = &ac
	dst, _ := s.blobs.EnvPath(req.Env, req.Digest)
	a.CopyDstPath = &dst

	verified, copyErr := s.blobs.CopyToEnv(ctx, req.Env, req.Digest)
	if copyErr != nil {
		stage := StageCopy
		status := "copy_failed"
		if errors.Is(copyErr, blob.ErrVerify) {
			status = "copy_failed" // 校验不过：副本损坏，拒绝晋级
		}
		return s.errOutcome(ctx, a, status, stage,
			"artifact copy or post-copy verification failed: "+copyErr.Error()), nil
	}
	a.CopiedVerifiedDigest = &verified
	if verified != req.Digest {
		return s.errOutcome(ctx, a, "copy_failed", StageCopy,
			fmt.Sprintf("verified digest mismatch: %s != %s", verified, req.Digest)), nil
	}

	return s.commitPromotion(ctx, a, req, v, req.ApprovalID)
}

func (s *Service) commitPromotion(ctx context.Context, a store.AttemptRow,
	req PromoteRequest, v *validated, approvalID *string) (Outcome, error) {

	// 先构造收据（gen_before/after 在 expected_gen 基础上确定；事务内会再次强校验）
	genBefore := req.ExpectedGen
	genAfter := genBefore + 1
	rcpt := receipt.Receipt{
		Kind:             "promote",
		AttemptID:        a.ID,
		Env:              req.Env,
		Digest:           req.Digest,
		GenBefore:        genBefore,
		GenAfter:         genAfter,
		EvidenceID:       req.EvidenceID,
		EvidenceVersion:  req.EvidenceVer,
		PolicyID:         req.PolicyID,
		PolicyVersion:    req.PolicyVer,
		PolicyBodySHA256: v.po.BodySHA256,
		CopyVerified:     *a.CopiedVerifiedDigest,
	}
	if approvalID != nil {
		rcpt.ApprovalID = *approvalID
	}
	signed, serr := s.signer.Sign(rcpt)
	if serr != nil {
		return Outcome{}, serr
	}
	rcptRaw, _ := json.Marshal(signed)
	rcptStr := string(rcptRaw)

	// 故障注入：副本已校验、指针事务提交前“崩溃”（仅测试，见 blob.WithFault）
	blob.CrashHook(ctx)

	policyID, policyVer := req.PolicyID, req.PolicyVer
	evID, evVer := req.EvidenceID, req.EvidenceVer
	in := store.CommitInput{
		Env:                  req.Env,
		NewDigest:            req.Digest,
		CopiedVerifiedDigest: *a.CopiedVerifiedDigest,
		ExpectedGen:          req.ExpectedGen,
		ChangeType:           "promote",
		AttemptID:            a.ID,
		PolicyID:             &policyID,
		PolicyVersion:        &policyVer,
		EvidenceID:           &evID,
		EvidenceVersion:      &evVer,
		ApprovalID:           approvalID,
		ReceiptJSON:          &rcptStr,
	}
	gotBefore, gotAfter, err := s.db.PromotionCommit(ctx, in)
	if err != nil {
		switch {
		case errors.Is(err, store.ErrConflict):
			return s.errOutcome(ctx, a, "conflict", StageCommit,
				fmt.Sprintf("environment gen moved: expected %d, current %d", req.ExpectedGen, gotBefore)), nil
		case errors.Is(err, store.ErrApprovalConsumed), errors.Is(err, store.ErrApprovalNotValid):
			return s.errOutcome(ctx, a, "rejected", StageCommit,
				"approval cannot be consumed: "+err.Error()), nil
		default:
			// 提交失败（含进程在事务中被杀死后外界看到的情形）：旧指针仍在，尝试保持 in_progress，由恢复收尾
			return Outcome{}, fmt.Errorf("pointer commit failed: %w", err)
		}
	}

	var asMap map[string]any
	_ = json.Unmarshal(rcptRaw, &asMap)
	return Outcome{AttemptID: a.ID, Status: "committed", Env: req.Env,
		Gen: &gotAfter, Receipt: asMap}, nil
}

func (s *Service) newAttemptRow(req PromoteRequest, status string) store.AttemptRow {
	return store.AttemptRow{
		ID:               newUUID(),
		Env:              req.Env,
		IdemKey:          req.IdempotencyKey,
		RequestedDigest:  req.Digest,
		SourceEvidenceID: &req.EvidenceID,
		EvidenceVersion:  &req.EvidenceVer,
		PolicyID:         &req.PolicyID,
		PolicyVersion:    &req.PolicyVer,
		ApprovalID:       req.ApprovalID,
		ExpectedGen:      req.ExpectedGen,
		Status:           status,
	}
}

func (s *Service) attemptToOutcome(ctx context.Context, a *store.AttemptRow) (Outcome, error) {
	o := Outcome{AttemptID: a.ID, Status: a.Status, Env: a.Env}
	if a.GenAfter != nil {
		o.Gen = a.GenAfter
	}
	if a.FailureReason != nil {
		o.Reason = *a.FailureReason
	}
	if a.ReceiptJSON != nil {
		var m map[string]any
		if err := json.Unmarshal([]byte(*a.ReceiptJSON), &m); err == nil {
			o.Receipt = m
		}
	}
	return o, nil
}
