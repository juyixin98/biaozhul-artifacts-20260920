package service

import (
	"context"
	"log"
	"time"
)

// Scheduler periodically persists overdue reminders. Because reminders are
// durable rows keyed by (action_item_id, due_version), restarting the
// process only means the first sweep after boot catches up on every missed
// deadline — nothing is held in memory, so a due item is never lost and the
// same version is never reminded twice.
type Scheduler struct {
	svc      *Service
	interval time.Duration
	batch    int32
}

func NewScheduler(svc *Service, interval time.Duration, batch int32) *Scheduler {
	if interval <= 0 {
		interval = 10 * time.Second
	}
	if batch <= 0 {
		batch = 100
	}
	return &Scheduler{svc: svc, interval: interval, batch: batch}
}

// Run blocks until ctx is canceled. It performs a catch-up sweep immediately
// (so reminders that came due while the service was down are persisted) and
// then on every tick.
func (sc *Scheduler) Run(ctx context.Context) {
	log.Printf("scheduler: starting with interval %s", sc.interval)
	sc.sweep(ctx)
	t := time.NewTicker(sc.interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			log.Printf("scheduler: stopping")
			return
		case <-t.C:
			sc.sweep(ctx)
		}
	}
}

func (sc *Scheduler) sweep(ctx context.Context) {
	if _, err := sc.svc.SweepDueOnce(ctx, sc.batch); err != nil {
		if ctx.Err() != nil {
			return
		}
		log.Printf("scheduler: sweep failed: %v", err)
	}
}
