// Package worker runs the background lifecycle loops.
//
// Two jobs tick on injected intervals:
//
//   - domain sweep  : registered->expired->redeemable->pending_delete->released
//   - transfer tick : approval timeouts (release frozen credits) and completion
//     after the simulated 5-day wait.
//
// Cross-process safety: each tick first takes a session-level Postgres
// advisory lock. Multiple replicas may run; only one executes a given tick,
// the rest skip. Within the process, jobs are idempotent and restart-safe —
// every row transition is its own committed transaction, so killing the
// process mid-run and restarting simply continues with no double effects.
package worker

import (
	"context"
	"log"
	"time"

	clk "domainengine/internal/clock"
	"domainengine/internal/domains"
	"domainengine/internal/transfers"
)

const (
	lockSweep     int64 = 91001
	lockTransfers int64 = 91002
)

type Worker struct {
	db      lockTaker
	domains *domains.Service
	xfer    *transfers.Service
	clock   clk.Clock
	sweep   time.Duration
	poll    time.Duration
	log     *log.Logger
}

type lockTaker interface {
	// WithSessionLock runs fn only if the session advisory lock was acquired.
	// It returns ran=false when another session currently holds the lock.
	WithSessionLock(ctx context.Context, id int64, fn func() error) (ran bool, err error)
}

func New(db lockTaker, d *domains.Service, x *transfers.Service, clock clk.Clock,
	sweepInterval, pollInterval time.Duration, logger *log.Logger) *Worker {
	return &Worker{
		db: db, domains: d, xfer: x, clock: clock,
		sweep: sweepInterval, poll: pollInterval, log: logger,
	}
}

// Run blocks until ctx is canceled, ticking both loops.
func (w *Worker) Run(ctx context.Context) {
	// Run once immediately so a freshly restarted process resumes pending
	// work without waiting an interval.
	w.tickSweep(ctx)
	w.tickTransfers(ctx)
	ticker := func(interval time.Duration, tick func(context.Context)) *time.Ticker {
		if interval <= 0 {
			interval = time.Minute
		}
		return time.NewTicker(interval)
	}
	sw := ticker(w.sweep, w.tickSweep)
	tr := ticker(w.poll, w.tickTransfers)
	defer sw.Stop()
	defer tr.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-sw.C:
			w.tickSweep(ctx)
		case <-tr.C:
			w.tickTransfers(ctx)
		}
	}
}

func (w *Worker) tickSweep(ctx context.Context) {
	w.withLock(ctx, lockSweep, "domain-sweep", func() {
		n, err := w.domains.Sweep(ctx, w.clock)
		if err != nil {
			w.log.Printf("sweep error: %v", err)
			return
		}
		if n > 0 {
			w.log.Printf("sweep: %d domain(s) transitioned", n)
		}
	})
}

func (w *Worker) tickTransfers(ctx context.Context) {
	w.withLock(ctx, lockTransfers, "transfer-tick", func() {
		n, err := w.xfer.ProcessDue(ctx, w.clock)
		if err != nil {
			w.log.Printf("transfer tick error: %v", err)
			return
		}
		if n > 0 {
			w.log.Printf("transfer-tick: %d transfer(s) processed", n)
		}
	})
}

func (w *Worker) withLock(ctx context.Context, id int64, name string, fn func()) {
	ran, err := w.db.WithSessionLock(ctx, id, func() error {
		fn()
		return nil
	})
	if err != nil {
		w.log.Printf("%s: lock error: %v", name, err)
		return
	}
	if !ran {
		return // another replica owns this tick
	}
}
