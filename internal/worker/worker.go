// Package worker runs the job-consumption loop: it claims queued jobs with
// leases, heartbeats them, drives one Chromium per job (bounded by a
// semaphore), and commits fenced success/failure reports. A single bad task is
// isolated and never blocks the queue.
package worker

import (
	"context"
	"fmt"
	"log"
	"os"
	"sync"
	"time"

	"sitevitals/internal/budget"
	"sitevitals/internal/collector"
	"sitevitals/internal/config"
	"sitevitals/internal/models"
	"sitevitals/internal/policy"
	"sitevitals/internal/store"
)

// Executor performs one collection. It is an interface so tests can inject
// fakes without launching a browser.
type Executor interface {
	Collect(ctx context.Context, targetURL string, opt collector.Options) (*collector.Result, error)
}

// chromeExecutor delegates to the real Chromium-backed collector.
type chromeExecutor struct{}

func (chromeExecutor) Collect(ctx context.Context, targetURL string, opt collector.Options) (*collector.Result, error) {
	return collector.Collect(ctx, targetURL, opt)
}

// Pool owns the worker goroutines and the browser-concurrency semaphore.
type Pool struct {
	cfg         config.Config
	q           *store.Queue
	repo        *store.Repo
	exec        Executor
	ev          *budget.Evaluator
	holderID    string
	browserSema chan struct{}

	policyMu    sync.RWMutex
	policyCache *policy.Checker
	cacheAt     time.Time
	policyTTL   time.Duration

	// CrashAfterLoad is a test-only hook forwarded to the collector.
	CrashAfterLoad bool
}

// NewPool constructs a worker pool.
func NewPool(cfg config.Config, q *store.Queue, repo *store.Repo, ev *budget.Evaluator, exec Executor) *Pool {
	if exec == nil {
		exec = chromeExecutor{}
	}
	host, _ := os.Hostname()
	id := fmt.Sprintf("%s-%d-%d", host, os.Getpid(), time.Now().UnixNano()%1_000_000)
	return &Pool{
		cfg:         cfg,
		q:           q,
		repo:        repo,
		exec:        exec,
		ev:          ev,
		holderID:    id,
		browserSema: make(chan struct{}, cfg.MaxBrowsers),
		policyTTL:   2 * time.Second,
	}
}

// checker returns a freshly-compiled allow-list, refreshing at most per policyTTL.
func (p *Pool) checker(ctx context.Context) (*policy.Checker, error) {
	p.policyMu.RLock()
	if p.policyCache != nil && time.Since(p.cacheAt) < p.policyTTL {
		c := p.policyCache
		p.policyMu.RUnlock()
		return c, nil
	}
	p.policyMu.RUnlock()

	sites, rules, err := p.repo.ActivePolicyData(ctx)
	if err != nil {
		return nil, err
	}
	c, err := policy.Compile(sites, rules)
	if err != nil {
		return nil, err
	}
	p.policyMu.Lock()
	p.policyCache, p.cacheAt = c, time.Now()
	p.policyMu.Unlock()
	return c, nil
}

// Run starts the worker goroutines plus the lease reaper and blocks until ctx
// is cancelled.
func (p *Pool) Run(ctx context.Context) {
	log.Printf("worker pool starting: workers=%d max_browsers=%d holder=%s",
		p.cfg.Workers, p.cfg.MaxBrowsers, p.holderID)
	var wg sync.WaitGroup
	for i := 0; i < p.cfg.Workers; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			p.workerLoop(ctx, id)
		}(i)
	}
	wg.Add(1)
	go func() { defer wg.Done(); p.reapLoop(ctx) }()
	wg.Wait()
}

// reapLoop periodically returns jobs whose leases expired (crash recovery).
func (p *Pool) reapLoop(ctx context.Context) {
	t := time.NewTicker(p.cfg.ReapInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-t.C:
			n, err := p.q.ReapExpired(ctx, now.UTC())
			if err != nil {
				log.Printf("reaper error: %v", err)
				continue
			}
			if n > 0 {
				log.Printf("reaper recovered %d expired lease(s) back to queue", n)
			}
		}
	}
}

func (p *Pool) workerLoop(ctx context.Context, id int) {
	idle := time.NewTimer(0)
	defer idle.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-idle.C:
		}
		wait := p.processOne(ctx, id)
		idle.Reset(wait)
	}
}

// processOne handles at most one job. It returns the idle delay before the
// next poll (empty queue backs off; errors retry immediately).
func (p *Pool) processOne(ctx context.Context, workerID int) time.Duration {
	claim, err := p.q.ClaimJob(ctx, p.holderID, p.cfg.LeaseTTL)
	if err != nil {
		if err == store.ErrNoJob {
			return 2 * time.Second
		}
		log.Printf("[w%d] claim error: %v", workerID, err)
		return time.Second
	}

	// The browser semaphore is acquired AFTER claiming so the lease covers
	// queuing for a browser slot, with heartbeats keeping it alive.
	hb := newHeartbeat(ctx, p.q, claim.Job.ID, claim.Run.FencingToken, p.holderID,
		p.cfg.HeartbeatInterval, p.cfg.LeaseTTL)
	hb.start()
	defer hb.stop()

	select {
	case p.browserSema <- struct{}{}:
	case <-ctx.Done():
		// Shutting down while waiting for a browser: release the lease for
		// another process/attempt by reporting a recoverable failure.
		_, _ = p.q.ReportFailure(ctx, claim.Job.ID, claim.Run.ID, claim.Run.FencingToken,
			p.holderID, models.FailCollector, "shutdown while waiting for browser slot")
		return 0
	}
	releaseBrowser := func() { <-p.browserSema }

	p.execute(ctx, claim, hb, releaseBrowser, workerID)
	return 0
}

func (p *Pool) execute(ctx context.Context, claim *store.Claim, hb *heartbeat,
	releaseBrowser func(), workerID int) {
	defer releaseBrowser()

	checker, err := p.checker(ctx)
	if err != nil {
		p.fail(ctx, claim, models.FailCollector, "compile allow-list: "+err.Error(), workerID)
		return
	}
	// Re-validate the target at execution time too (policy may have changed).
	if v := checker.Check(claim.Job.TargetURL); v != nil {
		p.fail(ctx, claim, models.FailPolicy, v.Error(), workerID)
		return
	}

	jobCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() {
		select {
		case <-hb.lost():
			log.Printf("[w%d] job %d lease lost (fence %d); cancelling browser work",
				workerID, claim.Job.ID, claim.Run.FencingToken)
			cancel()
		case <-jobCtx.Done():
		}
	}()

	res, err := p.exec.Collect(jobCtx, claim.Job.TargetURL, collector.Options{
		ExecPath:       p.cfg.ChromeBin,
		Viewport:       claim.Job.Viewport,
		Checker:        checker,
		MaxRedirects:   p.cfg.MaxRedirects,
		NavTimeout:     p.cfg.NavTimeout,
		SettleDelay:    2 * time.Second,
		CrashAfterLoad: p.CrashAfterLoad,
	})
	if err != nil {
		class, msg := models.FailCollector, err.Error()
		var ce *collector.CollectError
		if asCollectError(err, &ce) {
			class, msg = ce.Class, ce.Message
		}
		p.fail(ctx, claim, class, msg, workerID)
		return
	}

	accepted, err := p.q.ReportSuccess(ctx, claim.Job.ID, claim.Run.ID, claim.Run.FencingToken,
		p.holderID, &store.SuccessReport{
			FinalURL:         res.FinalURL,
			RedirectHops:     res.RedirectHops,
			Metrics:          res.Metrics,
			Resources:        res.Resources,
			Events:           res.Events,
			ResourceFailures: res.ResourceFailures,
			BlockedResources: res.BlockedResources,
		})
	if err != nil {
		log.Printf("[w%d] job %d report error: %v", workerID, claim.Job.ID, err)
		return
	}
	if !accepted {
		log.Printf("[w%d] job %d stale success report discarded (fence %d)",
			workerID, claim.Job.ID, claim.Run.FencingToken)
		return
	}
	log.Printf("[w%d] job %d succeeded run=%d redirects=%d blocked=%d resource_fails=%d",
		workerID, claim.Job.ID, claim.Run.ID, res.RedirectHops, res.BlockedResources, res.ResourceFailures)

	// Budget evaluation happens only after the report is accepted.
	if p.ev != nil {
		run := &models.Run{
			ID: claim.Run.ID, JobID: claim.Job.ID,
			TargetURL: claim.Job.TargetURL, Viewport: claim.Job.Viewport,
		}
		evals, berr := p.ev.Evaluate(ctx, run)
		if berr != nil {
			log.Printf("[w%d] job %d budget evaluation error: %v", workerID, claim.Job.ID, berr)
			return
		}
		for _, ev := range evals {
			if ev.Exceeded {
				log.Printf("[w%d] BUDGET ALERT job=%d run=%d %s/%s %s actual=%s threshold=%s",
					workerID, claim.Job.ID, claim.Run.ID, ev.TargetURL, ev.Viewport, ev.Metric,
					actualString(&ev), thresholdString(&ev))
			}
		}
	}
}

func (p *Pool) fail(ctx context.Context, claim *store.Claim, class, msg string, workerID int) {
	accepted, err := p.q.ReportFailure(ctx, claim.Job.ID, claim.Run.ID, claim.Run.FencingToken,
		p.holderID, class, msg)
	if err != nil {
		log.Printf("[w%d] job %d failure report error: %v", workerID, claim.Job.ID, err)
		return
	}
	if accepted {
		log.Printf("[w%d] job %d attempt %d failed (%s): %s",
			workerID, claim.Job.ID, claim.Run.Attempt, class, truncLog(msg))
	} else {
		log.Printf("[w%d] job %d stale failure report discarded (fence %d)",
			workerID, claim.Job.ID, claim.Run.FencingToken)
	}
}

func actualString(ev *models.BudgetEvaluation) string {
	if ev.ActualMS != nil {
		return fmt.Sprintf("%.1fms", *ev.ActualMS)
	}
	if ev.ActualCLS != nil {
		return fmt.Sprintf("%.4f", *ev.ActualCLS)
	}
	return "n/a"
}

func thresholdString(ev *models.BudgetEvaluation) string {
	if ev.ThresholdMS != nil {
		return fmt.Sprintf("%.1fms", *ev.ThresholdMS)
	}
	if ev.ThresholdCLS != nil {
		return fmt.Sprintf("%.4f", *ev.ThresholdCLS)
	}
	return "n/a"
}

func truncLog(s string) string {
	if len(s) > 200 {
		return s[:200]
	}
	return s
}
