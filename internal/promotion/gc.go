package promotion

import (
	"context"
	"errors"
	"fmt"

	"atomicpromo/internal/policy"
	"atomicpromo/internal/store"
)

// GCResult 一次垃圾回收的依据与动作
type GCResult struct {
	Env          string   `json:"env"`
	PolicyID     string   `json:"policy_id"`
	PolicyVer    int64    `json:"policy_version"`
	KeepDigests  []string `json:"keep_digests"`
	RemovedBlobs []string `json:"removed_blobs"`
	SkippedInuse []string `json:"skipped_inuse"`
}

// CollectGarbage 按钉版策略的保留规则回收环境副本：
// keep = 当前指针 + 最近 keep_last_n 个不同 digest 的完整历史；其余 env blob 删除。
// 历史行永不删除（审计需要）。回退时若目标已被 GC，则因不满足保留规则被拒绝。
func (s *Service) CollectGarbage(ctx context.Context, env, policyID string, policyVer int64) (GCResult, error) {
	if !validEnv(env) {
		return GCResult{}, fmt.Errorf("%w: invalid env", ErrBadRequest)
	}
	po, err := s.db.GetPolicy(ctx, policyID, policyVer)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return GCResult{}, fmt.Errorf("%w: policy version not found", ErrPolicyReject)
		}
		return GCResult{}, err
	}
	pol, err := policy.UnmarshalBody(po.BodyJSON)
	if err != nil {
		return GCResult{}, err
	}
	res := GCResult{Env: env, PolicyID: policyID, PolicyVer: policyVer}

	// 数据库删除在单事务内，并取与指针切换相同的环境咨询锁：
	// 锁内读历史/副本、重算保留集，杜绝“GC 与回退/晋级并发时凭旧快照误删”。
	tx, err := s.db.Pool.Begin(ctx)
	if err != nil {
		return GCResult{}, err
	}
	var lockID int64
	if err := tx.QueryRow(ctx, `SELECT hashtextextended($1, 0)`, env).Scan(&lockID); err != nil {
		tx.Rollback(ctx)
		return GCResult{}, err
	}
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, lockID); err != nil {
		tx.Rollback(ctx)
		return GCResult{}, err
	}
	histRows, err := s.db.ListHistoryTx(ctx, tx, env)
	if err != nil {
		tx.Rollback(ctx)
		return GCResult{}, err
	}
	existingRows, err := s.db.EnvBlobDigestsTx(ctx, tx, env)
	if err != nil {
		tx.Rollback(ctx)
		return GCResult{}, err
	}
	keep := computeKeepSet(histRows, pol.KeepLastN)
	for d := range keep {
		res.KeepDigests = append(res.KeepDigests, d)
	}
	sortStrings(res.KeepDigests)
	for _, d := range existingRows {
		if keep[d] {
			res.SkippedInuse = append(res.SkippedInuse, d)
			continue
		}
		if err := store.DeleteEnvBlob(ctx, tx, env, d); err != nil {
			tx.Rollback(ctx)
			return GCResult{}, err
		}
		res.RemovedBlobs = append(res.RemovedBlobs, d)
	}
	if _, err := s.db.InsertGCRun(ctx, store.GCRunRow{
		PolicyID: policyID, PolicyVersion: policyVer,
		KeepDigests: res.KeepDigests, RemovedBlobs: res.RemovedBlobs,
		SkippedInuse: res.SkippedInuse,
	}); err != nil {
		tx.Rollback(ctx)
		return GCResult{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return GCResult{}, err
	}
	// 物理文件在事务成功后删（回滚不会留下账实不符）
	for _, d := range res.RemovedBlobs {
		if err := s.blobs.RemoveEnvBlob(env, d); err != nil {
			return res, fmt.Errorf("committed gc but physical removal failed for %s: %w", d, err)
		}
	}
	sortStrings(res.RemovedBlobs)
	sortStrings(res.SkippedInuse)
	return res, nil
}

func sortStrings(x []string) {
	for i := 1; i < len(x); i++ {
		for j := i; j > 0 && x[j-1] > x[j]; j-- {
			x[j-1], x[j] = x[j], x[j-1]
		}
	}
}
