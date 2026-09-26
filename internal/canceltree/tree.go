package canceltree

import (
	"context"
	"errors"
	"sync"
	"time"
)

// Outcome of a single leaf as observed by the orchestrator.
type Outcome string

const (
	// OutcomeSucceeded: Run returned nil.
	OutcomeSucceeded Outcome = "succeeded"
	// OutcomeCanceled: the leaf was stopped because the tree was canceled
	// (a fatal sibling or a root/client disconnect).
	OutcomeCanceled Outcome = "canceled"
	// OutcomeDeadlineExceeded: the leaf was stopped by a deadline imposed
	// from above (the tree/root context).
	OutcomeDeadlineExceeded Outcome = "deadline_exceeded"
	// OutcomeFailed: the leaf returned an error of its own (including its
	// own internally imposed timeout).
	OutcomeFailed Outcome = "failed"
	// OutcomePanicked: the leaf panicked; the value is captured as the error.
	OutcomePanicked Outcome = "panicked"
)

// CleanupOutcome reports how a leaf's cleanup finished.
type CleanupOutcome string

const (
	CleanupNotNeeded CleanupOutcome = "not_needed"
	CleanupRan       CleanupOutcome = "ran"
	CleanupFailed    CleanupOutcome = "failed"
)

// LeafReport is the structured per-leaf result.
type LeafReport struct {
	Name              string         `json:"name"`
	Fatal             bool           `json:"fatal"`
	Outcome           Outcome        `json:"outcome"`
	Error             string         `json:"error,omitempty"`
	StartedAtMS       int64          `json:"started_at_ms"`
	FinishedAtMS      int64          `json:"finished_at_ms"`
	DurationMS        int64          `json:"duration_ms"`
	CancelObservedMS  int64          `json:"cancel_observed_at_ms,omitempty"`
	Cleanup           CleanupOutcome `json:"cleanup"`
	CleanupError      string         `json:"cleanup_error,omitempty"`
	CleanupDurationMS int64          `json:"cleanup_duration_ms"`
}

// Report is the structured result of one tree execution.
type Report struct {
	OK             bool         `json:"ok"`
	Canceled       bool         `json:"canceled"` // root context was canceled (e.g. client disconnect)
	FatalTrigger   string       `json:"fatal_trigger,omitempty"`
	Error          string       `json:"error,omitempty"`
	StartedAtMS    int64        `json:"started_at_ms"`
	FinishedAtMS   int64        `json:"finished_at_ms"`
	DurationMS     int64        `json:"duration_ms"`
	TimeToCancelMS int64        `json:"time_to_cancel_ms,omitempty"` // root start -> first fatal/root cancel
	Leaves         []LeafReport `json:"leaves"`
}

// Runner is a leaf's work. It MUST honor ctx.Done and return promptly once
// canceled.
type Runner func(ctx context.Context) error

// Cleanup releases resources acquired by a leaf.
type Cleanup func(ctx context.Context) error

// Leaf is one subtask.
type Leaf struct {
	Name string
	// Fatal means an own error from this leaf cancels the tree. Non-fatal
	// leaves are best-effort: their errors are recorded only.
	Fatal bool
	// Run does the work. Required.
	Run Runner
	// Cleanup, if non-nil, is always invoked after Run returns and always
	// awaited, including after cancellation. It receives the tree context,
	// which may already be canceled; well-behaved cleanup still releases.
	Cleanup Cleanup
}

// Tree executes leaves concurrently as a cancellation tree.
type Tree struct {
	leaves []Leaf
	now    func() time.Time
}

// New builds a tree from leaves. Leaves all start together on Run.
func New(leaves ...Leaf) *Tree {
	return &Tree{leaves: leaves, now: time.Now}
}

// WithClock overrides the clock used for timestamps. The tree itself does
// not sleep; the clock is only for structured timing.
func (t *Tree) WithClock(n func() time.Time) *Tree {
	t.now = n
	return t
}

// trigger records the first fatal event under a single Once.
type trigger struct {
	once sync.Once
	atMS int64
	name string
	err  string
}

func (tr *trigger) fire(nowMS int64) {
	tr.once.Do(func() { tr.atMS = nowMS })
}

// Run executes all leaves under ctx. It blocks until all leaves and their
// cleanups have completed.
func (t *Tree) Run(ctx context.Context) Report {
	start := t.now()
	rep := Report{StartedAtMS: start.UnixMilli(), Leaves: []LeafReport{}}

	treeCtx, treeCancel := context.WithCancel(ctx)
	defer treeCancel()

	tr := &trigger{}

	// Root cancellation (client disconnect, or a parent deadline) propagates
	// to the whole tree. This watcher is explicitly awaited before Run reads
	// trigger fields, so its writes under the Once have a happens-before
	// edge with those reads (the watcher is NOT covered by the leaf WG).
	var watcherWG sync.WaitGroup
	watcherWG.Add(1)
	go func() {
		defer watcherWG.Done()
		select {
		case <-ctx.Done():
			tr.fire(t.now().UnixMilli())
			treeCancel()
		case <-treeCtx.Done():
		}
	}()

	var wg sync.WaitGroup
	reports := make([]LeafReport, len(t.leaves))
	for i, leaf := range t.leaves {
		wg.Add(1)
		go func(i int, leaf Leaf) {
			defer wg.Done()
			reports[i] = t.runLeaf(treeCtx, leaf, tr, treeCancel)
		}(i, leaf)
	}
	wg.Wait()

	// Release and join the root watcher before observing trigger state.
	treeCancel()
	watcherWG.Wait()

	finish := t.now()
	rep.FinishedAtMS = finish.UnixMilli()
	rep.Leaves = reports
	rep.DurationMS = finish.Sub(start).Milliseconds()
	if tr.atMS > 0 {
		rep.TimeToCancelMS = tr.atMS - start.UnixMilli()
	}

	t.fillSummary(ctx, tr, &rep)
	return rep
}

func (t *Tree) runLeaf(ctx context.Context, leaf Leaf, tr *trigger, treeCancel context.CancelFunc) LeafReport {
	lr := LeafReport{Name: leaf.Name, Fatal: leaf.Fatal, Cleanup: CleanupNotNeeded}
	start := t.now()
	lr.StartedAtMS = start.UnixMilli()

	runErr := safeRun(ctx, leaf.Run)

	if ctx.Err() != nil {
		lr.CancelObservedMS = t.now().UnixMilli()
	}
	lr.classify(runErr, ctx)

	// A leaf's OWN error is fatal when the leaf is marked fatal (its own
	// internal timeout counts) or when it panicked — a panic is never safe
	// to ignore. An error observed only because the tree was already
	// stopping (ctx.Err() != nil) is a consequence, not a trigger.
	_, isPanic := runErr.(*PanicError)
	ownError := runErr != nil && ctx.Err() == nil
	if ownError && (leaf.Fatal || isPanic) {
		tr.once.Do(func() {
			tr.atMS = t.now().UnixMilli()
			tr.name = leaf.Name
			tr.err = runErr.Error()
		})
		treeCancel()
	}

	finish := t.now()
	lr.FinishedAtMS = finish.UnixMilli()
	lr.DurationMS = finish.Sub(start).Milliseconds()

	// Cleanup always runs and is always awaited, even after cancellation.
	if leaf.Cleanup != nil {
		cStart := t.now()
		cleanupErr := safeCleanup(ctx, leaf.Cleanup)
		cEnd := t.now()
		lr.CleanupDurationMS = cEnd.Sub(cStart).Milliseconds()
		if cleanupErr != nil {
			lr.Cleanup = CleanupFailed
			lr.CleanupError = cleanupErr.Error()
		} else {
			lr.Cleanup = CleanupRan
		}
	}
	return lr
}

// classify assigns the outcome, distinguishing an own failure from being
// stopped by tree/root cancellation.
func (lr *LeafReport) classify(err error, ctx context.Context) {
	switch {
	case err == nil:
		lr.Outcome = OutcomeSucceeded
	case isPanicErr(err):
		lr.Outcome = OutcomePanicked
		lr.Error = err.Error()
	case errors.Is(err, context.DeadlineExceeded) && errors.Is(ctx.Err(), context.DeadlineExceeded):
		lr.Outcome = OutcomeDeadlineExceeded
		lr.Error = err.Error()
	case errors.Is(err, context.Canceled) && ctx.Err() != nil:
		lr.Outcome = OutcomeCanceled
		lr.Error = err.Error()
	default:
		// Own timeout, own cancellation, or any other error is the leaf's
		// own failure unless the tree was already stopping.
		if ctx.Err() != nil {
			lr.Outcome = OutcomeCanceled
		} else {
			lr.Outcome = OutcomeFailed
		}
		lr.Error = err.Error()
	}
}

func isPanicErr(err error) bool {
	_, ok := err.(*PanicError)
	return ok
}

// safeRun invokes r, converting a panic into an error.
func safeRun(ctx context.Context, r Runner) (err error) {
	defer func() {
		if rec := recover(); rec != nil {
			err = &PanicError{V: rec, msg: "leaf panicked: " + toString(rec)}
		}
	}()
	return r(ctx)
}

func safeCleanup(ctx context.Context, c Cleanup) (err error) {
	defer func() {
		if rec := recover(); rec != nil {
			err = &PanicError{V: rec, msg: "cleanup panicked: " + toString(rec), inCleanup: true}
		}
	}()
	return c(ctx)
}

func toString(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	if e, ok := v.(error); ok {
		return e.Error()
	}
	return "panic"
}

// PanicError captures a panicked leaf or cleanup.
type PanicError struct {
	V         any
	msg       string
	inCleanup bool
}

func (e *PanicError) Error() string { return e.msg }

func (t *Tree) fillSummary(rootCtx context.Context, tr *trigger, rep *Report) {
	switch {
	case rootCtx.Err() != nil:
		// Client disconnect or a parent deadline: the request was aborted.
		rep.Canceled = true
		rep.OK = false
		rep.Error = "root context canceled: " + rootCtx.Err().Error()
	case tr.name != "":
		rep.OK = false
		rep.FatalTrigger = tr.name
		rep.Error = tr.err
	default:
		// No root abort and no fatal leaf failed. Best-effort (non-fatal)
		// failures are tolerated and remain visible only in the leaf list.
		rep.OK = true
	}
}
