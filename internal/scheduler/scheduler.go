// Package scheduler runs the periodic background jobs. Every tick is
// idempotent and guarded by a MySQL named lock, so duplicate ticks, process
// restarts and multiple replicas never double-process. Unprocessed events
// live in the database (events.processed=0), so a crash mid-batch loses
// nothing: the next tick (and the sweep backstop) picks the rows up.
package scheduler

import (
	"context"
	"log"
	"time"

	"anomalywatch/internal/config"
	"anomalywatch/internal/detection"
)

type Scheduler struct {
	engine *detection.Engine
	cfg    config.Config
}

func New(engine *detection.Engine, cfg config.Config) *Scheduler {
	return &Scheduler{engine: engine, cfg: cfg}
}

// Run blocks until ctx is canceled, ticking the three jobs.
func (s *Scheduler) Run(ctx context.Context) {
	// Stagger the first ticks slightly so jobs don't pile up at boot.
	process := time.NewTicker(s.cfg.ProcessInterval)
	sweep := time.NewTicker(s.cfg.SweepInterval)
	escalate := time.NewTicker(s.cfg.EscalateInterval)
	defer process.Stop()
	defer sweep.Stop()
	defer escalate.Stop()

	// Run one processing pass shortly after boot to drain any backlog left by
	// an unclean shutdown.
	s.runProcessOnce(ctx)

	for {
		select {
		case <-ctx.Done():
			return
		case <-process.C:
			s.runProcessOnce(ctx)
		case <-sweep.C:
			s.runSweepOnce(ctx)
		case <-escalate.C:
			s.runEscalateOnce(ctx)
		}
	}
}

func (s *Scheduler) runProcessOnce(ctx context.Context) {
	// Drain in a loop: one tick should clear whatever a batch-ingest burst
	// left behind, bounded by a safety iteration cap.
	for i := 0; i < 100; i++ {
		if ctx.Err() != nil {
			return
		}
		n, err := s.engine.ProcessPending(s.cfg.ProcessBatch)
		if err != nil {
			log.Printf("scheduler: process pending: %v", err)
			return
		}
		if n == 0 {
			return
		}
	}
}

func (s *Scheduler) runSweepOnce(ctx context.Context) {
	ran, err := s.engine.WithJobLock(ctx, "job:sweep", func() error {
		return s.engine.RunSweep(ctx)
	})
	if err != nil {
		log.Printf("scheduler: sweep: %v", err)
		return
	}
	if ran {
		log.Printf("scheduler: sweep complete")
	}
}

func (s *Scheduler) runEscalateOnce(ctx context.Context) {
	ran, err := s.engine.WithJobLock(ctx, "job:escalate", func() error {
		n, innerErr := s.engine.EscalateOverdue()
		if innerErr == nil && n > 0 {
			log.Printf("scheduler: auto-escalated %d alert(s)", n)
		}
		return innerErr
	})
	if err != nil {
		log.Printf("scheduler: escalate: %v", err)
		return
	}
	_ = ran
}
