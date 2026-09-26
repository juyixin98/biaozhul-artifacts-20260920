// Package shutdown implements the four-phase graceful shutdown state machine:
//
//	STOP_ACCEPT -> DRAINING -> CANCELLING -> CLOSING -> CLOSED
//
// The coordinator is clock-injectable (see internal/clock) so phase timeouts
// are deterministic in tests. It is safe for concurrent use and repeated
// shutdown signals are idempotent but escalate the active wait.
package shutdown

import (
	"context"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"gracefulshutdown/internal/clock"
	"gracefulshutdown/internal/lifecycle"
)

// Config holds shutdown phase budgets. Zero durations mean "do not wait".
type Config struct {
	DrainTimeout  time.Duration
	CancelTimeout time.Duration
	CloseTimeout  time.Duration
	Clk           clock.Clock
	// TimerArmed, if set, is called right after a phase-wait timer is armed.
	// Tests with a fake clock use it to advance the clock only after the
	// coordinator is actually waiting on the timer.
	TimerArmed func()
}

// Resource is anything that must be closed during phase 4. Resources close in
// reverse registration order (last registered closes first).
type Resource interface {
	Name() string
	Close(ctx context.Context) error
}

// Handle identifies an accepted unit of work.
type Handle struct {
	id      string
	kind    string
	started time.Time
}

// Coordinator owns the shutdown phase machine and structured reporting.
type Coordinator struct {
	cfg Config

	mu       sync.Mutex
	phase    lifecycle.Phase
	started  bool
	signals  int
	timeline []lifecycle.Event
	records  []lifecycle.RequestRecord

	wg         sync.WaitGroup
	active     atomic.Int64
	bgSpawned  int
	bgRejected int

	stopAccept chan struct{}
	cancelWork chan struct{}
	escMu      sync.Mutex
	escCh      chan struct{} // latched closed once by the first repeat signal
	escClosed  bool
	done       chan struct{}

	stopHooks []func()
	resources []Resource

	subs      []chan lifecycle.Report
	finalized bool
	report    lifecycle.Report

	finalizeWg sync.WaitGroup
}

// New builds a coordinator. If no clock is supplied the real clock is used.
func New(cfg Config) *Coordinator {
	if cfg.Clk == nil {
		cfg.Clk = clock.Real{}
	}
	return &Coordinator{
		cfg:        cfg,
		phase:      lifecycle.PhaseRunning,
		stopAccept: make(chan struct{}),
		cancelWork: make(chan struct{}),
		escCh:      make(chan struct{}),
		done:       make(chan struct{}),
	}
}

// OnStopAccept registers a hook fired exactly once at phase 1 (listener close).
func (c *Coordinator) OnStopAccept(h func()) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stopHooks = append(c.stopHooks, h)
}

// ExpectFinalizeAck declares that a trigger will flush the finalized report
// and call FinalizeAck afterwards. The run waits for all acks before closing
// Done, so the process cannot exit underneath a response still being written
// on a hijacked (untracked) connection. Returns false if the report is already
// finalized (no ack is then required). Call BEFORE the starting signal.
func (c *Coordinator) ExpectFinalizeAck() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.finalized {
		return false
	}
	c.finalizeWg.Add(1)
	return true
}

// FinalizeAck marks one expected flush as done.
func (c *Coordinator) FinalizeAck() { c.finalizeWg.Done() }

// RegisterResource adds a phase-4 resource (closed in reverse order, so the
// last registered resource closes first).
func (c *Coordinator) RegisterResource(r Resource) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.resources = append(c.resources, r)
}

// Phase returns the current phase.
func (c *Coordinator) Phase() lifecycle.Phase {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.phase
}

// StopAccepting is closed at phase 1.
func (c *Coordinator) StopAccepting() <-chan struct{} { return c.stopAccept }

// Cancelled is closed at phase 3; request contexts derive from it.
func (c *Coordinator) Cancelled() <-chan struct{} { return c.cancelWork }

// Done is closed when the full shutdown sequence (incl. finalize hooks) ends.
func (c *Coordinator) Done() <-chan struct{} { return c.done }

// Begin accepts a foreground unit of work. It returns false once phase 1 has
// begun; the caller must reject the work (HTTP 503) and do nothing else.
func (c *Coordinator) Begin(id, kind string) (Handle, bool) {
	c.mu.Lock()
	if c.phase != lifecycle.PhaseRunning {
		now := c.cfg.Clk.Now()
		c.records = append(c.records, lifecycle.RequestRecord{
			ID: id, Kind: kind, Outcome: lifecycle.OutcomeRejected,
			StartedAt: now, FinishedAt: now,
			Detail: "rejected: server no longer accepting work",
		})
		c.mu.Unlock()
		return Handle{}, false
	}
	started := c.cfg.Clk.Now()
	c.mu.Unlock()

	c.wg.Add(1)
	c.active.Add(1)
	return Handle{id: id, kind: kind, started: started}, true
}

// Finish records the definitive outcome of an accepted unit of work.
func (c *Coordinator) Finish(h Handle, outcome lifecycle.Outcome, detail string) {
	finished := c.cfg.Clk.Now()
	c.mu.Lock()
	c.records = append(c.records, lifecycle.RequestRecord{
		ID: h.id, Kind: h.kind, Outcome: outcome,
		StartedAt: h.started, FinishedAt: finished, Detail: detail,
	})
	c.mu.Unlock()
	c.active.Add(-1)
	c.wg.Done()
}

// BeginBackground admits a background task only while still accepting. After
// STOP_ACCEPT it always returns false: no new background tasks may be spawned.
func (c *Coordinator) BeginBackground(id string) (Handle, bool) {
	c.mu.Lock()
	if c.phase != lifecycle.PhaseRunning {
		c.bgRejected++
		c.mu.Unlock()
		return Handle{}, false
	}
	c.bgSpawned++
	started := c.cfg.Clk.Now()
	c.mu.Unlock()

	c.wg.Add(1)
	c.active.Add(1)
	return Handle{id: id, kind: "background", started: started}, true
}

// FinishBackground mirrors Finish for background tasks.
func (c *Coordinator) FinishBackground(h Handle, outcome lifecycle.Outcome, detail string) {
	c.Finish(h, outcome, detail)
}

// Signal delivers one shutdown signal and blocks until the full sequence
// completes, returning the structured report. The first signal runs the four
// phases; further signals are counted and escalate the active drain/cancel
// wait.
func (c *Coordinator) Signal() lifecycle.Report {
	c.NotifySignal()
	<-c.done
	return c.Report()
}

// NotifySignal delivers one shutdown signal without blocking. The first signal
// starts the four phases; repeats are counted and escalate the active wait.
// Use SubscribeReport to observe completion without holding a signal goroutine.
func (c *Coordinator) NotifySignal() {
	c.mu.Lock()
	c.signals++
	n := c.signals
	start := !c.started
	if start {
		c.started = true
	}
	c.mu.Unlock()

	if start {
		c.spawnRun(n)
		return
	}

	// Repeat signal: latch a single escalation channel closed once. Any wait
	// in progress (and any subsequent wait) observes it and hurries to the
	// next phase — i.e. duplicate signals mean "finish shutdown as fast as
	// possible". Repeats collapse into the one latch and can never panic.
	c.escMu.Lock()
	if !c.escClosed {
		c.escClosed = true
		close(c.escCh)
	}
	c.escMu.Unlock()
}

// SubscribeReport returns a channel that receives exactly one finalized report.
// Subscribing after finalization delivers the report immediately.
func (c *Coordinator) SubscribeReport() <-chan lifecycle.Report {
	ch := make(chan lifecycle.Report, 1)
	c.mu.Lock()
	if c.finalized {
		r := c.report
		c.mu.Unlock()
		ch <- r
		return ch
	}
	c.subs = append(c.subs, ch)
	c.mu.Unlock()
	return ch
}

func (c *Coordinator) spawnRun(firstSignal int) {
	go func() {
		defer close(c.done)
		start := c.cfg.Clk.Now()

		// Phase 1: stop accepting new work / cut traffic.
		c.transition(lifecycle.PhaseStopAccept,
			fmt.Sprintf("signal #%d received; cutting traffic", firstSignal))
		c.mu.Lock()
		hooks := append([]func(){}, c.stopHooks...)
		c.mu.Unlock()
		for _, h := range hooks {
			h()
		}

		// Phase 2: drain accepted work up to the drain budget.
		c.transition(lifecycle.PhaseDraining,
			fmt.Sprintf("waiting up to %s for %d in-flight unit(s)", c.cfg.DrainTimeout, c.active.Load()))
		if !c.waitWork(c.cfg.DrainTimeout, "drain budget elapsed") {
			// Phase 3: cancel work still in flight.
			c.transition(lifecycle.PhaseCancelling,
				fmt.Sprintf("cancelling remaining %d unit(s); budget %s", c.active.Load(), c.cfg.CancelTimeout))
			close(c.cancelWork)
			c.waitWork(c.cfg.CancelTimeout, "cancel budget elapsed")
		} else {
			c.transition(lifecycle.PhaseCancelling, "all work drained; nothing left to cancel")
			close(c.cancelWork)
		}

		// Phase 4: close resources in reverse registration order.
		c.transition(lifecycle.PhaseClosing,
			fmt.Sprintf("closing resources; %d unit(s) still active", c.active.Load()))
		closeOrder := c.closeResources()

		c.transition(lifecycle.PhaseClosed, "shutdown complete")
		finished := c.cfg.Clk.Now()
		report := c.buildReport(start, finished, closeOrder)
		c.publishReport(report)
		// Wait for triggering requests to flush the report before Done closes.
		c.finalizeWg.Wait()
	}()
}

// publishReport hands the finalized report to every subscriber.
func (c *Coordinator) publishReport(r lifecycle.Report) {
	c.mu.Lock()
	subs := c.subs
	c.subs = nil
	c.mu.Unlock()
	for _, ch := range subs {
		ch <- r
	}
}

// waitWork blocks until all accepted work finishes, the budget elapses
// (clock-driven), or a repeat signal escalates. Returns true if work drained.
func (c *Coordinator) waitWork(budget time.Duration, timeoutNote string) bool {
	workDone := make(chan struct{})
	go func() {
		c.wg.Wait()
		close(workDone)
	}()

	var budgetCh <-chan time.Time
	if budget > 0 {
		budgetCh = c.armTimer(budget)
	}
	select {
	case <-workDone:
		return true
	case <-budgetCh:
		c.appendNote(timeoutNote)
		return false
	case <-c.escCh:
		c.appendNote("repeat signal: escalating to next phase")
		return false
	}
}

func (c *Coordinator) transition(p lifecycle.Phase, note string) {
	c.mu.Lock()
	c.phase = p
	c.timeline = append(c.timeline, lifecycle.Event{Phase: p, At: c.cfg.Clk.Now(), Note: note})
	if p == lifecycle.PhaseStopAccept {
		close(c.stopAccept)
	}
	c.mu.Unlock()
}

func (c *Coordinator) appendNote(note string) {
	c.mu.Lock()
	c.timeline = append(c.timeline, lifecycle.Event{Phase: c.phase, At: c.cfg.Clk.Now(), Note: note})
	c.mu.Unlock()
}

// closeResources shuts resources down in reverse registration order.
func (c *Coordinator) closeResources() []lifecycle.CloseRecord {
	c.mu.Lock()
	res := append([]Resource{}, c.resources...)
	c.mu.Unlock()

	records := make([]lifecycle.CloseRecord, 0, len(res))
	order := 0
	for i := len(res) - 1; i >= 0; i-- {
		r := res[i]
		order++
		start := c.cfg.Clk.Now()
		err := c.closeOne(r)
		rec := lifecycle.CloseRecord{
			Name:       r.Name(),
			Order:      order,
			At:         start,
			Active:     c.active.Load(),
			DurationMS: c.cfg.Clk.Now().Sub(start).Milliseconds(),
		}
		if err != nil {
			rec.Err = err.Error()
		}
		records = append(records, rec)
	}
	return records
}

func (c *Coordinator) closeOne(r Resource) error {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- r.Close(ctx) }()

	if c.cfg.CloseTimeout <= 0 {
		return <-done
	}
	select {
	case err := <-done:
		return err
	case <-c.armTimer(c.cfg.CloseTimeout):
		cancel()
		err := <-done
		if err != nil {
			return fmt.Errorf("close timed out after %s: %w", c.cfg.CloseTimeout, err)
		}
		return fmt.Errorf("close timed out after %s", c.cfg.CloseTimeout)
	}
}

// armTimer arms a clock-driven wait and fires the test hook afterwards.
func (c *Coordinator) armTimer(d time.Duration) <-chan time.Time {
	ch := c.cfg.Clk.After(d)
	if c.cfg.TimerArmed != nil {
		c.cfg.TimerArmed()
	}
	return ch
}

func (c *Coordinator) buildReport(start, finished time.Time, closeOrder []lifecycle.CloseRecord) lifecycle.Report {
	c.mu.Lock()
	defer c.mu.Unlock()

	r := lifecycle.Report{
		StartedAt:                start,
		FinishedAt:               finished,
		DurationMS:               finished.Sub(start).Milliseconds(),
		Signals:                  c.signals,
		Timeline:                 append([]lifecycle.Event{}, c.timeline...),
		Requests:                 append([]lifecycle.RequestRecord{}, c.records...),
		CloseOrder:               closeOrder,
		InFlightAtClose:          int(c.active.Load()),
		BackgroundSpawned:        c.bgSpawned,
		BackgroundAfterStop:      0, // invariant: BeginBackground rejects after STOP_ACCEPT
		RejectedBackgroundSpawns: c.bgRejected,
	}
	for _, rec := range c.records {
		switch rec.Outcome {
		case lifecycle.OutcomeCompleted:
			r.Completed++
		case lifecycle.OutcomeCancelled:
			r.Cancelled++
		case lifecycle.OutcomeRejected:
			r.Rejected++
		}
	}
	c.finalized = true
	c.report = r
	return r
}

// Report returns the finalized report after CLOSED, or a live snapshot before.
func (c *Coordinator) Report() lifecycle.Report {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.started && c.phase == lifecycle.PhaseClosed {
		return c.report
	}
	r := lifecycle.Report{
		Signals:                  c.signals,
		Timeline:                 append([]lifecycle.Event{}, c.timeline...),
		Requests:                 append([]lifecycle.RequestRecord{}, c.records...),
		BackgroundSpawned:        c.bgSpawned,
		RejectedBackgroundSpawns: c.bgRejected,
	}
	for _, rec := range c.records {
		switch rec.Outcome {
		case lifecycle.OutcomeCompleted:
			r.Completed++
		case lifecycle.OutcomeCancelled:
			r.Cancelled++
		case lifecycle.OutcomeRejected:
			r.Rejected++
		}
	}
	return r
}
