package service

import (
	"context"
	"fmt"
)

// RecoveryReport describes what restart recovery did.
type RecoveryReport struct {
	FenceEpoch      int64    `json:"fenceEpoch"`
	ToUncertain     []string `json:"toUncertain"`
	StillWaiting    []string `json:"stillWaiting"`
	KeptRunningHold []string `json:"keptRunningHold"` // running after recovery (none expected without a live worker)
	Note            string   `json:"note"`
}

// Recover runs once at startup.
//
//  1. The fence epoch is bumped: any worker that completes or heartbeats
//     with a token from a previous epoch is rejected (fencing).
//  2. All 'running' tasks move to 'uncertain': after a crash the scheduler
//     cannot know whether the physical robot stopped. Their resources stay
//     held — fenced, never reallocated — until a late completion arrives
//     (which is accepted even in uncertain state) or an operator confirms
//     the stop.
//  3. 'waiting' tasks remain queued; their wanted rows rebuild the wait-for
//     graph automatically.
func (s *Service) Recover(ctx context.Context) (*RecoveryReport, error) {
	rep := &RecoveryReport{}

	epoch, err := s.nextEpoch(ctx)
	if err != nil {
		return nil, err
	}
	rep.FenceEpoch = epoch

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)

	rows, err := tx.Query(ctx,
		`UPDATE tasks SET state='uncertain', lease_expires=NULL, updated_at=now()
		 WHERE state='running' RETURNING id`)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return nil, err
		}
		rep.ToUncertain = append(rep.ToUncertain, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	for _, id := range rep.ToUncertain {
		s.logEvent(ctx, tx, id, "restart_recovery",
			fmt.Sprintf("epoch %d: running -> uncertain; resources stay fenced", epoch))
	}

	wrows, err := tx.Query(ctx, `SELECT id FROM tasks WHERE state='waiting' ORDER BY enqueued_at, id`)
	if err != nil {
		return nil, err
	}
	for wrows.Next() {
		var id string
		if err := wrows.Scan(&id); err != nil {
			wrows.Close()
			return nil, err
		}
		rep.StillWaiting = append(rep.StillWaiting, id)
	}
	wrows.Close()
	if err := wrows.Err(); err != nil {
		return nil, err
	}

	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	rep.Note = "running holders are uncertain and keep their resources; late completion is accepted, otherwise stop confirmation is required"
	return rep, nil
}

func (s *Service) nextEpoch(ctx context.Context) (int64, error) {
	var epoch int64
	err := s.pool.QueryRow(ctx,
		`INSERT INTO meta(key,value,updated_at)
		 VALUES ('fence_epoch','1', now())
		 ON CONFLICT (key) DO UPDATE
		   SET value = (meta.value::bigint + 1)::text,
		       updated_at = now()
		 RETURNING value::bigint`).Scan(&epoch)
	if err != nil {
		return 0, err
	}
	if _, err := s.pool.Exec(ctx,
		`UPDATE tasks SET fence_epoch = $1 WHERE state='running'`, epoch); err != nil {
		return 0, err
	}
	return epoch, nil
}
