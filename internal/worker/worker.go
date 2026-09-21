// Package worker runs background settlement and reconciliation loops.
// Both jobs are idempotent and crash-safe: interrupted runs leave a
// 'processing'/'running' row that is taken over, and completed rows are reused.
package worker

import (
	"context"
	"log"
	"time"

	"github.com/google/uuid"

	"github.com/clearsettle/clearsettle/internal/audit"
	"github.com/clearsettle/clearsettle/internal/recon"
	"github.com/clearsettle/clearsettle/internal/settle"
)

type Worker struct {
	Settle         *settle.Service
	Recon          *recon.Service
	SettleHorizon  time.Duration
	SettleInterval time.Duration
	ReconInterval  time.Duration
	Tag            string
}

func New(s *settle.Service, rc *recon.Service, tag string, horizon time.Duration) *Worker {
	return &Worker{
		Settle:         s,
		Recon:          rc,
		SettleHorizon:  horizon,
		SettleInterval: time.Minute,
		ReconInterval:  time.Minute,
		Tag:            tag,
	}
}

func systemActor() audit.Entry {
	return audit.Entry{ActorRole: "system", Detail: map[string]any{"worker": true}}
}

// Run blocks until ctx is canceled, ticking settlement and reconciliation.
// An initial run happens immediately at startup so a crashed job resumes fast.
func (w *Worker) Run(ctx context.Context) {
	st := time.NewTicker(w.SettleInterval)
	rt := time.NewTicker(w.ReconInterval)
	defer st.Stop()
	defer rt.Stop()

	w.settleOnce(ctx)
	w.reconOnce(ctx)
	for {
		select {
		case <-ctx.Done():
			log.Printf("worker %s stopping", w.Tag)
			return
		case <-st.C:
			w.settleOnce(ctx)
		case <-rt.C:
			w.reconOnce(ctx)
		}
	}
}

func (w *Worker) settleOnce(ctx context.Context) {
	defer func() {
		if rec := recover(); rec != nil {
			log.Printf("settlement sweep panic: %v", rec)
		}
	}()
	results, err := w.Settle.RunSweep(ctx, time.Now(), w.SettleHorizon, nil, systemActor())
	if err != nil {
		log.Printf("settlement sweep error: %v", err)
		return
	}
	for _, r := range results {
		log.Printf("settled merchant=%s day=%s net=%d payments=%d",
			r.MerchantID, r.Date.Format("2006-01-02"), r.NetAmount, r.PaymentCount)
	}
}

func (w *Worker) reconOnce(ctx context.Context) {
	defer func() {
		if rec := recover(); rec != nil {
			log.Printf("reconciliation sweep panic: %v", rec)
		}
	}()
	// Only yesterday is complete; a "today" run can be triggered manually and
	// is reusable, but it must not be frozen by the automatic loop.
	day := time.Now().UTC().Add(-24 * time.Hour)
	results, err := w.Recon.RunSweep(ctx, day, systemActor())
	if err != nil {
		log.Printf("recon sweep error for %s: %v", day.Format("2006-01-02"), err)
		return
	}
	for _, r := range results {
		if r.Discrepancies > 0 {
			log.Printf("RECON DISCREPANCY merchant=%s day=%s count=%d",
				r.MerchantID, r.Date, r.Discrepancies)
		}
	}
}

// RunOnce executes one sweep of each job and returns (used by CLI mode).
func (w *Worker) RunOnce(ctx context.Context) (settled []settle.DayResult, reconRuns []recon.RunSummary, err error) {
	settled, err = w.Settle.RunSweep(ctx, time.Now(), w.SettleHorizon, nil, systemActor())
	if err != nil {
		return
	}
	reconRuns, err = w.Recon.RunSweep(ctx, time.Now().UTC().Add(-24*time.Hour), systemActor())
	return
}

var _ = uuid.Nil
