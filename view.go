package agingqueue

import "time"

// viewLocked 在持锁状态下生成作业快照。
func (s *Scheduler) viewLocked(j *Job, now time.Time) *JobView {
	v := &JobView{
		ID:                j.id,
		Type:              j.typ,
		Priority:          j.priority,
		EffectivePriority: j.effectivePriority(now, s.cfg.AgingStep),
		State:             j.state,
		Attempts:          j.attempts,
		MaxAttempts:       j.maxAttempts,
		EnqueuedAt:        j.enqueuedAt,
		ReadySince:        j.readySince,
		StartedAt:         j.startedAt,
		FinishedAt:        j.finishedAt,
		LastError:         j.lastErr,
		Seq:               j.seq,
	}
	if len(j.result) > 0 {
		v.Result = string(j.result)
	}
	if j.state == StateDelayed && j.delayedRef != nil {
		v.NextRetryAt = j.delayedRef.readyAt
	}
	if j.state == StateQueued {
		v.EffectivePriority = j.curPriority
	}
	return v
}
