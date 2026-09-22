// Package jobs runs the periodic lifecycle maintenance pass.
package jobs

import (
	"context"
	"log"
	"time"

	"domainengine/internal/service"
)

// Runner executes RunMaintenance on a ticker. The pass itself is idempotent
// and crash-safe, so the runner needs no state of its own: after a restart
// it simply runs again, and overlapping runs are harmless.
type Runner struct {
	svc      *service.Service
	interval time.Duration
	logger   *log.Logger
}

func NewRunner(svc *service.Service, interval time.Duration, logger *log.Logger) *Runner {
	return &Runner{svc: svc, interval: interval, logger: logger}
}

func (r *Runner) Run(ctx context.Context) {
	ticker := time.NewTicker(r.interval)
	defer ticker.Stop()
	r.pass(ctx)
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			r.pass(ctx)
		}
	}
}

func (r *Runner) pass(ctx context.Context) {
	rep, err := r.svc.RunMaintenance(ctx)
	if err != nil {
		r.logger.Printf("maintenance: %v", err)
		return
	}
	if rep.Expired+rep.ToRedemption+rep.ToPendingDelete+rep.Deleted+rep.TransfersComplete+rep.TransfersTimedOut > 0 {
		r.logger.Printf("maintenance: %+v", rep)
	}
}
