package promotion

import (
	"context"

	"atomicpromo/internal/store"
)

// RecoveryReport 启动恢复结论
type RecoveryReport struct {
	RecoveredCommitted []string `json:"recovered_committed"`
	RecoveredAborted   []string `json:"recovered_aborted"`
	PendingApprovals   []string `json:"pending_approvals"`
}

// RecoverOnStartup 处理上次进程留下的未完成尝试：
//   - in_progress 且指针其实已经指向 requested_digest（事务在崩溃前已提交）→ recovered_committed，
//     补齐尝试状态与收据（收据丢失时只记录事实，不伪造）；
//   - in_progress 但指针未切换（提交前崩溃）→ recovered_aborted；
//     旧指针仍然可用，副本保留（下次同代次重试幂等复用并重新校验）；
//   - awaiting_approval 原样保留，等审批到达。
func (s *Service) RecoverOnStartup(ctx context.Context) (RecoveryReport, error) {
	var rep RecoveryReport
	unfinished, err := s.db.ListUnfinishedAttempts(ctx)
	if err != nil {
		return rep, err
	}
	for _, a := range unfinished {
		if a.Status == "awaiting_approval" {
			rep.PendingApprovals = append(rep.PendingApprovals, a.ID)
			continue
		}
		ptr, err := s.db.GetPointer(ctx, a.Env)
		pointed := err == nil && ptr.CurrentDigest != nil && *ptr.CurrentDigest == a.RequestedDigest
		if err != nil && err != store.ErrNotFound {
			return rep, err
		}
		if pointed {
			if err := s.db.MarkRecovered(ctx, a.ID, "recovered_committed",
				"pointer already switched at crash; promotion took effect"); err != nil {
				return rep, err
			}
			rep.RecoveredCommitted = append(rep.RecoveredCommitted, a.ID)
		} else {
			if err := s.db.MarkRecovered(ctx, a.ID, "recovered_aborted",
				"crashed before pointer commit; previous pointer remains in service; retry with same expected_gen"); err != nil {
				return rep, err
			}
			rep.RecoveredAborted = append(rep.RecoveredAborted, a.ID)
		}
	}
	return rep, nil
}
