// Package shutdown implements the four-phase graceful shutdown coordinator:
//
//  1. receiving  -> stop accepting (readiness flips, new work rejected,
//     no new background tasks may be spawned)
//  2. draining   -> wait for in-flight accepted work to finish, bounded by
//     the drain timeout
//  3. cancelling -> cancel the contexts of everything still in flight
//  4. closing    -> close registered resources in LIFO order
//
// Shutdown is idempotent: repeated shutdown signals observe the same run.
package shutdown

import (
	"context"
	"sync"
	"sync/atomic"
	"time"

	"graceful-shutdown/internal/clock"
	"graceful-shutdown/internal/ledger"
	"graceful-shutdown/internal/resources"
)

// Phase names the coordinator's current stage.
type Phase string

const (
	PhaseReceiving  Phase = "receiving"
	PhaseDraining   Phase = "draining"
	PhaseCancelling Phase = "cancelling"
	PhaseClosing    Phase = "closing"
	PhaseDone       Phase = "done"
)

// PhaseTransition records when a phase was entered.
type PhaseTransition struct {
	Phase Phase     `json:"phase"`
	At    time.Time `json:"at"`
}

// State is the structured snapshot exposed via /state.
type State struct {
	Phase            Phase                   `json:"phase"`
	Accepting        bool                    `json:"accepting"`
	InFlight         int64                   `json:"in_flight"`
	DrainTimedOut    bool                    `json:"drain_timed_out"`
	ShutdownSignals  uint64                  `json:"shutdown_signals"`
	RejectedRequests uint64                  `json:"rejected_requests"`
	RejectedTasks    uint64                  `json:"rejected_tasks"`
	PhaseLog         []PhaseTransition       `json:"phase_log"`
	CloseOrder       []resources.CloseRecord `json:"close_order"`
	Ledger           []ledger.Entry          `json:"ledger"`
}

// Coordinator gates new work and drives the four shutdown phases.
type Coordinator struct {
	clk          clock.Clock
	drainTimeout time.Duration
	ledger       *ledger.Ledger
	registry     *resources.Registry

	mu         sync.Mutex
	phase      Phase
	phaseLog   []PhaseTransition
	started    bool
	drainedOK  bool
	timedOut   bool
	doneCh     chan struct{}
	rootCtx    context.Context
	rootCancel context.CancelFunc

	inflight    sync.WaitGroup
	inflightN   atomic.Int64
	signals     atomic.Uint64
	rejectedReq atomic.Uint64
	rejectedTsk atomic.Uint64
}

// New builds a Coordinator. drainTimeout bounds the draining phase before
// the cancelling phase forcibly cancels remaining work.
func New(clk clock.Clock, drainTimeout time.Duration, led *ledger.Ledger, reg *resources.Registry) *Coordinator {
	ctx, cancel := context.WithCancel(context.Background())
	c := &Coordinator{
		clk:          clk,
		drainTimeout: drainTimeout,
		ledger:       led,
		registry:     reg,
		phase:        PhaseReceiving,
		doneCh:       make(chan struct{}),
		rootCtx:      ctx,
		rootCancel:   cancel,
	}
	c.phaseLog = append(c.phaseLog, PhaseTransition{Phase: PhaseReceiving, At: clk.Now()})
	return c
}

// Accepting reports whether new work is still being accepted (readiness).
func (c *Coordinator) Accepting() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.phase == PhaseReceiving
}

// TryAccept admits one unit of work (HTTP request or background task).
// It fails once the receiving phase has ended, which is what guarantees no
// new background tasks are spawned after stop-accepting. On success the
// caller must eventually call Finish exactly once.
func (c *Coordinator) TryAccept(path, kind string) (uint64, context.Context, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.phase != PhaseReceiving {
		if kind == "background-task" {
			c.rejectedTsk.Add(1)
		} else {
			c.rejectedReq.Add(1)
		}
		return 0, nil, false
	}
	c.inflight.Add(1)
	c.inflightN.Add(1)
	id := c.ledger.Begin(path, kind, c.clk.Now())
	return id, c.rootCtx, true
}

// Finish records the terminal outcome of an accepted unit of work.
func (c *Coordinator) Finish(id uint64, outcome ledger.Outcome, detail string) {
	c.ledger.End(id, c.clk.Now(), outcome, detail)
	c.inflightN.Add(-1)
	c.inflight.Done()
}

// Shutdown triggers the four-phase shutdown. It is synchronous through the
// transition into draining: once it returns, TryAccept is guaranteed to
// reject all new work (including background tasks). It is idempotent —
// every call (including repeated signals) returns the same done channel,
// which is closed when the closing phase completes.
func (c *Coordinator) Shutdown() <-chan struct{} {
	c.signals.Add(1)
	c.mu.Lock()
	if c.started {
		c.mu.Unlock()
		return c.doneCh
	}
	c.started = true
	// Phase 1 ends / phase 2 begins synchronously with the signal so that
	// "stop accepting" takes effect before Shutdown returns.
	c.phase = PhaseDraining
	c.phaseLog = append(c.phaseLog, PhaseTransition{Phase: PhaseDraining, At: c.clk.Now()})
	c.mu.Unlock()

	timer := c.clk.NewTimer(c.drainTimeout)
	go c.run(timer)
	return c.doneCh
}

// Done returns the channel closed when shutdown fully completes.
func (c *Coordinator) Done() <-chan struct{} { return c.doneCh }

func (c *Coordinator) transition(p Phase) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.phase = p
	c.phaseLog = append(c.phaseLog, PhaseTransition{Phase: p, At: c.clk.Now()})
}

func (c *Coordinator) run(timer clock.Timer) {
	// Already in phase 2 (draining): wait for in-flight work, bounded by
	// the drain timeout created synchronously in Shutdown.
	waitCh := make(chan struct{})
	go func() {
		c.inflight.Wait()
		close(waitCh)
	}()
	select {
	case <-waitCh:
		c.mu.Lock()
		c.drainedOK = true
		c.mu.Unlock()
	case <-timer.C():
		c.mu.Lock()
		c.timedOut = true
		c.mu.Unlock()
	}
	timer.Stop()

	// Phase 3: cancel anything still in flight and wait for it to unwind.
	c.transition(PhaseCancelling)
	c.rootCancel()
	<-waitCh

	// Phase 4: close resources in LIFO order.
	c.transition(PhaseClosing)
	c.registry.CloseAll(context.Background(), c.clk.Now)

	c.transition(PhaseDone)
	close(c.doneCh)
}

// State returns a consistent structured snapshot of the coordinator.
func (c *Coordinator) State() State {
	c.mu.Lock()
	defer c.mu.Unlock()
	phaseLog := make([]PhaseTransition, len(c.phaseLog))
	copy(phaseLog, c.phaseLog)
	return State{
		Phase:            c.phase,
		Accepting:        c.phase == PhaseReceiving,
		InFlight:         c.inflightN.Load(),
		DrainTimedOut:    c.timedOut,
		ShutdownSignals:  c.signals.Load(),
		RejectedRequests: c.rejectedReq.Load(),
		RejectedTasks:    c.rejectedTsk.Load(),
		PhaseLog:         phaseLog,
		CloseOrder:       c.registry.CloseLog(),
		Ledger:           c.ledger.Snapshot(),
	}
}
