package promotion

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"atomicpromo/internal/policy"
	"atomicpromo/internal/receipt"
	"atomicpromo/internal/store"
)

// Rollback 把环境指针回退到历史版本。
// 目标必须：(a) 在 env_history 中有完整晋级记录；(b) 环境副本仍在且重新哈希通过
// （副本若已被 GC 则重新从内容库复制+校验）；(c) 回退后保留规则仍满足。
// 并发回退与并发晋级一样由 expected_gen 拦截。
func (s *Service) Rollback(ctx context.Context, req RollbackRequest) (Outcome, error) {
	if req.IdempotencyKey != nil && *req.IdempotencyKey != "" {
		if got, err := s.db.GetAttemptByIdem(ctx, req.Env, *req.IdempotencyKey); err == nil {
			return s.attemptToOutcome(ctx, got)
		}
	}
	if !validEnv(req.Env) || req.ExpectedGen < 0 || req.PolicyID == "" || req.PolicyVer <= 0 {
		return Outcome{}, fmt.Errorf("%w: env, expected_gen and pinned policy version required", ErrBadRequest)
	}
	if (req.Digest == "") == (req.TargetGen == nil) {
		return Outcome{}, fmt.Errorf("%w: provide exactly one of digest or target_gen", ErrBadRequest)
	}

	// 用钉版策略重检保留规则（防止“回退到一个已不满足当前保留策略的版本”）
	po, err := s.db.GetPolicy(ctx, req.PolicyID, req.PolicyVer)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return Outcome{}, fmt.Errorf("%w: policy version not found", ErrPolicyReject)
		}
		return Outcome{}, err
	}
	pol, err := policy.UnmarshalBody(po.BodyJSON)
	if err != nil {
		return Outcome{}, err
	}
	if want, _ := pol.VersionHash(); want != po.BodySHA256 {
		return Outcome{}, fmt.Errorf("%w: policy body hash mismatch", ErrPolicyReject)
	}

	hist, err := s.db.ListHistory(ctx, req.Env)
	if err != nil {
		return Outcome{}, err
	}
	if len(hist) == 0 {
		return Outcome{}, fmt.Errorf("%w: environment has no promoted history", ErrRollbackTarget)
	}
	var target *store.HistoryRow
	if req.TargetGen != nil {
		for i := range hist {
			if hist[i].Gen == *req.TargetGen {
				target = &hist[i]
				break
			}
		}
	} else {
		if !validDigest(req.Digest) {
			return Outcome{}, fmt.Errorf("%w: digest must be sha256:<hex>", ErrImmutableRef)
		}
		// 指向该 digest 的最近一次历史
		for i := len(hist) - 1; i >= 0; i-- {
			if hist[i].Digest == req.Digest {
				target = &hist[i]
				break
			}
		}
	}
	if target == nil {
		return Outcome{}, fmt.Errorf("%w: target not found in environment history", ErrRollbackTarget)
	}
	if _, err := s.db.GetArtifact(ctx, target.Digest); err != nil {
		return Outcome{}, fmt.Errorf("%w: target artifact missing from canonical store", ErrRollbackTarget)
	}

	keep := computeKeepSet(hist, pol.KeepLastN)
	if !keep[target.Digest] {
		return Outcome{}, fmt.Errorf(
			"%w: target %s is not in the retention set of the pinned policy (keep_last_n=%d); rollback refused",
			ErrRetentionReject, target.Digest, pol.KeepLastN)
	}

	idemKey := req.IdempotencyKey
	a := store.AttemptRow{
		ID: newUUID(), Env: req.Env, IdemKey: idemKey,
		RequestedDigest: target.Digest,
		PolicyID:        &req.PolicyID, PolicyVersion: &req.PolicyVer,
		ExpectedGen: req.ExpectedGen, Status: "in_progress",
	}
	if target.EvidenceID != nil {
		a.SourceEvidenceID = target.EvidenceID
		a.EvidenceVersion = target.EvidenceVersion
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

	// 环境副本必须物理存在并能重新哈希通过。
	// 注意：不从内容库重新复制——被 GC 按保留规则清走的环境副本不允许“复活”，
	// 回退只能指向完整且仍满足保留规则的历史产物。
	exists, err := s.blobs.EnvBlobExists(req.Env, target.Digest)
	if err != nil {
		return Outcome{}, err
	}
	if !exists {
		// 仍落一条尝试证据
		idemKey := req.IdempotencyKey
		a0 := store.AttemptRow{
			ID: newUUID(), Env: req.Env, IdemKey: idemKey,
			RequestedDigest: target.Digest,
			PolicyID:        &req.PolicyID, PolicyVersion: &req.PolicyVer,
			ExpectedGen: req.ExpectedGen, Status: "rejected",
		}
		_ = s.db.InsertAttempt(ctx, a0)
		return Outcome{}, fmt.Errorf(
			"%w: env blob for %s was garbage-collected and is outside the retention set",
			ErrRollbackTarget, target.Digest)
	}
	verified, verr := s.blobs.CopyToEnv(ctx, req.Env, target.Digest)
	if verr != nil {
		return s.errOutcome(ctx, a, "copy_failed", StageCopy,
			"rollback target verification failed: "+verr.Error()), nil
	}
	a.CopiedVerifiedDigest = &verified

	genBefore, genAfter := req.ExpectedGen, req.ExpectedGen+1
	rcpt := receipt.Receipt{
		Kind: "rollback", AttemptID: a.ID, Env: req.Env, Digest: target.Digest,
		GenBefore: genBefore, GenAfter: genAfter,
		PolicyID: req.PolicyID, PolicyVersion: req.PolicyVer, PolicyBodySHA256: po.BodySHA256,
		CopyVerified: verified,
	}
	if target.EvidenceID != nil {
		rcpt.EvidenceID = *target.EvidenceID
		rcpt.EvidenceVersion = *target.EvidenceVersion
	}
	signed, serr := s.signer.Sign(rcpt)
	if serr != nil {
		return Outcome{}, serr
	}
	rcptRaw, _ := json.Marshal(signed)
	rcptStr := string(rcptRaw)

	in := store.CommitInput{
		Env:                  req.Env,
		NewDigest:            target.Digest,
		CopiedVerifiedDigest: verified,
		ExpectedGen:          req.ExpectedGen,
		ChangeType:           "rollback",
		AttemptID:            a.ID,
		PolicyID:             &req.PolicyID,
		PolicyVersion:        &req.PolicyVer,
		EvidenceID:           target.EvidenceID,
		EvidenceVersion:      target.EvidenceVersion,
		ReceiptJSON:          &rcptStr,
	}
	gotBefore, gotAfter, cerr := s.db.PromotionCommit(ctx, in)
	if cerr != nil {
		if errors.Is(cerr, store.ErrConflict) {
			return s.errOutcome(ctx, a, "conflict", StageCommit,
				fmt.Sprintf("concurrent change: expected gen %d, current %d", req.ExpectedGen, gotBefore)), nil
		}
		return Outcome{}, cerr
	}
	var asMap map[string]any
	_ = json.Unmarshal(rcptRaw, &asMap)
	return Outcome{AttemptID: a.ID, Status: "committed", Env: req.Env,
		Gen: &gotAfter, Receipt: asMap}, nil
}

// computeKeepSet 计算保留集合：当前指针 + 最近 keepLastN 个不同 digest 的历史版本。
func computeKeepSet(hist []store.HistoryRow, keepLastN int) map[string]bool {
	keep := map[string]bool{}
	if len(hist) == 0 {
		return keep
	}
	keep[hist[len(hist)-1].Digest] = true // 当前指针
	added := 1
	for i := len(hist) - 1; i >= 0 && added < keepLastN; i-- {
		d := hist[i].Digest
		if !keep[d] {
			keep[d] = true
			added++
		}
	}
	// keep_last_n 的语义是“至少保留 N 个版本”：N 更大时持续向前取
	if keepLastN > added {
		for i := len(hist) - 1; i >= 0; i-- {
			if !keep[hist[i].Digest] {
				keep[hist[i].Digest] = true
				added++
				if added >= keepLastN {
					break
				}
			}
		}
	}
	return keep
}
