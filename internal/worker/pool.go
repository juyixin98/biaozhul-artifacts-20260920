package worker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"sync"
	"time"

	"sitevitals/internal/browser"
	"sitevitals/internal/budget"
	"sitevitals/internal/compare"
	"sitevitals/internal/config"
	"sitevitals/internal/models"
	"sitevitals/internal/report"
	"sitevitals/internal/store"
	"sitevitals/internal/whitelist"
)

// Pool runs the durable task queue against a bounded number of local
// Chromium instances. A failure in one task never blocks the queue: each
// processing goroutine recovers panics, commits a classified failure, and
// loops. Stale leases are reclaimed so crashed processes' tasks are retaken.
type Pool struct {
	cfg   config.Config
	st    *store.Store
	insts []*browser.Instance
}

func New(cfg config.Config, st *store.Store, insts []*browser.Instance) *Pool {
	return &Pool{cfg: cfg, st: st, insts: insts}
}

// Run blocks until ctx is cancelled. Browser instances are already started.
func (p *Pool) Run(ctx context.Context) error {
	n := len(p.insts)
	if n == 0 {
		return errors.New("worker pool requires at least one browser instance")
	}
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(slot int) {
			defer wg.Done()
			p.workerLoop(ctx, slot)
		}(i)
	}

	// Background reaper: defensive in case all workers are saturated, this
	// returns expired leases to the queued state quickly.
	reaperStop := make(chan struct{})
	var rwg sync.WaitGroup
	rwg.Add(1)
	go func() {
		defer rwg.Done()
		t := time.NewTicker(15 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-t.C:
				if n, err := p.st.RequeueExpired(ctx); err != nil && !errors.Is(err, context.Canceled) {
					log.Printf("reaper: %v", err)
				} else if n > 0 {
					log.Printf("reaper: returned %d expired lease(s) to queue", n)
				}
			case <-reaperStop:
				return
			case <-ctx.Done():
				return
			}
		}
	}()

	<-ctx.Done()
	close(reaperStop)
	wg.Wait()
	rwg.Wait()
	return nil
}

func (p *Pool) workerLoop(ctx context.Context, slot int) {
	inst := p.insts[slot]
	poll := p.cfg.PollInterval
	if poll <= 0 {
		poll = 2 * time.Second
	}
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		claimed, err := p.st.Claim(ctx, p.cfg.WorkerID+"-"+fmt.Sprint(slot), p.cfg.LeaseDuration)
		if err != nil {
			if !errors.Is(err, context.Canceled) {
				log.Printf("worker[%d] claim error: %v", slot, err)
			}
			if !sleep(ctx, poll) {
				return
			}
			continue
		}
		if claimed == nil {
			if p.cfg.RunOnce {
				return
			}
			if !sleep(ctx, poll) {
				return
			}
			continue
		}
		p.process(ctx, slot, inst, claimed)
		if p.cfg.RunOnce {
			return
		}
	}
}

// process executes exactly one claimed task. It never panics out of the
// worker loop; a panic is recorded as an internal failure.
func (p *Pool) process(parent context.Context, slot int, inst *browser.Instance, claim *store.ClaimResult) {
	task := claim.Task
	run := claim.Run
	tag := fmt.Sprintf("worker[%d] task=%d attempt=%d", slot, task.ID, run.AttemptNo)

	defer func() {
		if r := recover(); r != nil {
			log.Printf("%s PANIC recovered: %v", tag, r)
			_ = p.commitFailure(parent, claim, browser.CodeInternal, fmt.Sprintf("worker panic: %v", r), nil)
		}
	}()

	prof, err := whitelist.Profile(task.Viewport)
	if err != nil {
		_ = p.commitFailure(parent, claim, browser.CodePolicyBlocked, err.Error(), nil)
		return
	}
	sites, err := p.st.ListSites(parent, false)
	if err != nil {
		log.Printf("%s: load whitelist: %v (retrying task later)", tag, err)
		_ = p.commitFailure(parent, claim, browser.CodeInternal, "load whitelist: "+err.Error(), nil)
		return
	}
	matcher := whitelist.NewMatcher(sites)

	// Heartbeat until collection finishes. Losing the lease means another
	// worker reclaimed us; we must abort and must not commit.
	hbCtx, hbCancel := context.WithCancel(parent)
	runCtx, runCancel := context.WithTimeout(hbCtx, p.cfg.TaskTimeout)
	defer runCancel()
	leaseLost := make(chan struct{})
	var hbWG sync.WaitGroup
	hbWG.Add(1)
	go func() {
		defer hbWG.Done()
		t := time.NewTicker(p.cfg.HeartbeatInterval)
		defer t.Stop()
		for {
			select {
			case <-t.C:
				if err := p.st.Heartbeat(hbCtx, task.ID, *task.LeaseToken, p.cfg.LeaseDuration); err != nil {
					if errors.Is(err, store.ErrLeaseLost) {
						log.Printf("%s: lease lost, aborting browser run", tag)
						close(leaseLost)
						runCancel() // stop the browser collection right away
						return
					}
					log.Printf("%s: heartbeat: %v", tag, err)
				}
			case <-hbCtx.Done():
				return
			}
		}
	}()

	collector := browser.NewCollector(inst, p.cfg.StrictSubresources)
	metrics, diag, cerr := func() (m *store.RunMetrics, d *browser.Diagnostics, e error) {
		defer func() {
			if r := recover(); r != nil {
				e = &browser.CollectError{Code: browser.CodeInternal, Message: fmt.Sprintf("collector panic: %v", r)}
			}
		}()
		return collector.Collect(runCtx, browser.CollectOptions{
			TargetURL:    task.URL,
			Profile:      prof,
			Matcher:      matcher,
			NavTimeout:   p.cfg.NavTimeout,
			Settle:       p.cfg.SettleTime,
			MaxRedirects: p.cfg.MaxRedirects,
		})
	}()

	// Browser may have crashed during the run; relaunch for the next task.
	select {
	case <-inst.Crashed():
		log.Printf("%s: browser instance crashed; restarting", tag)
		_ = inst.Restart(parent)
	default:
	}

	hbCancel()
	hbWG.Wait()

	select {
	case <-leaseLost:
		log.Printf("%s: dropping commit (lease was reclaimed)", tag)
		return
	default:
	}

	if cerr != nil {
		ce, ok := browser.AsCollectError(cerr)
		code, msg := browser.CodeInternal, cerr.Error()
		if ok {
			code, msg = ce.Code, ce.Message
		}
		log.Printf("%s FAILED %s: %s", tag, code, msg)
		_ = p.commitFailure(parent, claim, code, msg, diag)
		return
	}
	if err := p.commitSuccess(parent, claim, sites, metrics); err != nil {
		if errors.Is(err, store.ErrLeaseLost) {
			log.Printf("%s: late success commit rejected (lease lost)", tag)
			return
		}
		log.Printf("%s: commit success: %v", tag, err)
	}
}

func (p *Pool) commitFailure(ctx context.Context, claim *store.ClaimResult, code, msg string, diag *browser.Diagnostics) error {
	in := store.FailureInput{
		TaskID: claim.Task.ID, Token: deref(claim.Task.LeaseToken),
		RunID: claim.Run.ID, ErrorCode: code, ErrorMsg: msg,
	}
	if err := p.st.CommitFailure(ctx, in); err != nil && !errors.Is(err, store.ErrLeaseLost) {
		return err
	}
	// Persist diagnostics on the run too, where possible. Diagnostics live in
	// the runs JSON sidecars; a failed task keeps redirect/violation evidence.
	if diag != nil && (len(diag.Redirects) > 0 || len(diag.Violations) > 0 || len(diag.Warnings) > 0) {
		updates := map[string]interface{}{}
		if b, err := json.Marshal(diag.Redirects); err == nil {
			updates["redirects_json"] = string(b)
		}
		if b, err := json.Marshal(diag.Violations); err == nil {
			updates["violations_json"] = string(b)
		}
		if b, err := json.Marshal(diag.Warnings); err == nil {
			updates["warnings_json"] = string(b)
		}
		if len(updates) > 0 {
			_ = p.st.DB().Model(&models.Run{}).Where("id = ?", claim.Run.ID).Updates(updates).Error
		}
	}
	return nil
}

func (p *Pool) commitSuccess(ctx context.Context, claim *store.ClaimResult, sites []models.Site, m *store.RunMetrics) error {
	task, run := claim.Task, claim.Run
	siteID := matchSiteID(sites, m.FinalURL)
	bdg, err := p.st.EffectiveBudget(ctx, siteID)
	if err != nil {
		return fmt.Errorf("load budget: %w", err)
	}

	// We need the run row populated to evaluate budgets/alerts and render the
	// report; CommitSuccess writes it all transactionally, so compute rows
	// up-front and pass them through the WriteAlerts hook.
	windowEnd := m.WindowEnd
	populated := &models.Run{
		ID: run.ID, TaskID: task.ID, AttemptNo: run.AttemptNo,
		Status: models.StateSucceeded, Owner: run.Owner,
		Viewport: task.Viewport, URL: task.URL,
		FinalURL:            m.FinalURL,
		StartedAt:           run.StartedAt,
		WindowEnd:           &windowEnd,
		WindowStartOffsetMS: m.WindowStartOffsetMS,
		NavDurationMS:       m.NavDurationMS, FCPMS: m.FCPMS, LCPMS: m.LCPMS, CLS: m.CLS,
		LongTaskCount: m.LongTaskCount, LongTaskTotalMS: m.LongTaskTotalMS,
		LongTaskMaxMS: m.LongTaskMaxMS, MetricStatus: m.MetricStatus,
		Resources: m.Resources, Redirects: m.Redirects, Violations: m.Violations, Warnings: m.Warnings,
	}
	alerts := budget.Check(task.ID, run.ID, bdg, populated)
	md := report.Build(task, populated, alerts)

	// Auto-comparison against the previous successful run of same URL+viewport.
	var comp *models.Comparison
	if baseline, err := p.st.LatestSuccessfulRun(ctx, task.URL, task.Viewport, task.ID); err == nil {
		if verr := compare.ValidatePair(baseline, populated); verr == nil {
			diff := compare.Diff(baseline, populated)
			diffJSON, _ := json.Marshal(diff)
			comp = &models.Comparison{
				URL: task.URL, Viewport: task.Viewport,
				BaselineRun: baseline.ID, CurrentRun: run.ID,
				DiffJSON: string(diffJSON), Diff: diff,
			}
		}
	}

	return p.st.CommitSuccess(ctx, store.SuccessInput{
		TaskID:    task.ID,
		Token:     deref(task.LeaseToken),
		RunID:     run.ID,
		FinalURL:  m.FinalURL,
		Collected: m,
		ReportMD:  md,
		WriteAlerts: func(runID uint) error {
			for i := range alerts {
				alerts[i].RunID = runID
				if err := p.st.CreateAlert(ctx, &alerts[i]); err != nil {
					return err
				}
			}
			if comp != nil {
				return p.st.SaveComparison(ctx, comp)
			}
			return nil
		},
	})
}

// matchSiteID finds the registered site whose entry covers finalURL.
func matchSiteID(sites []models.Site, raw string) uint {
	u, err := whitelist.ParseHTTPURL(raw)
	if err != nil {
		return 0
	}
	for _, s := range sites {
		eu, err := whitelist.ParseHTTPURL(s.Origin)
		if err != nil || !s.Enabled {
			continue
		}
		if eu.Hostname() == u.Hostname() && eu.Port() == u.Port() {
			return s.ID
		}
	}
	return 0
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func sleep(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}
