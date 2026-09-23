// Package queue implements a discrete priority job queue with aging.
//
// Scheduling model
//
//   - Priorities are discrete integers in [MinPriority, MaxPriority]; larger
//     wins. A job's EFFECTIVE priority is BasePriority + floor(waited/
//     AgeInterval), capped at the job's MaxPriority.
//   - Waiting time accumulates while a job is queued, including retry
//     backoff. It is frozen only while an attempt is running.
//   - Same effective priority is FIFO by immutable submission sequence.
//   - Retries keep the original EnqueuedAt and submission sequence: a
//     failure can never reset a job's age or let it overtake jobs that were
//     ahead of it.
//
// All state changes produce ordered, structured Event records (see Sink).
package queue

import (
	"container/heap"
	"container/list"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"sync"
	"time"

	"agingqueue/clock"
)

// Executor runs one job attempt. Defined locally (rather than importing the
// executor package) to keep this package free of upward dependencies.
type Executor interface {
	Execute(ctx context.Context, j *Job) error
}

// BackoffPolicy computes the delay before attempt n (1-based; the call for
// attempt 2 returns the first backoff) after an error.
type BackoffPolicy interface {
	Backoff(attempt int) time.Duration
}

// Config configures a Scheduler. Zero values are replaced by defaults where
// noted; Validate reports the rest.
type Config struct {
	Clock       clock.Clock
	Executor    Executor
	Sink        Sink
	MinPriority int           // default 0
	MaxPriority int           // default 9
	AgeInterval time.Duration // waiting time per effective-priority level; default 1s
	Concurrency int           // parallel attempts; default 1
	Backoff     BackoffPolicy
	// DefaultMaxAttempts applies to submissions without MaxAttempts; default 3
	DefaultMaxAttempts int
	// IDGenerator supplies new job ids; default is 8 random hex bytes.
	IDGenerator func() (string, error)
}

// ExponentialBackoff waits Base*Factor^(attempt-2), capped at Max.
type ExponentialBackoff struct {
	Base   time.Duration
	Factor float64
	Max    time.Duration
}

func (b ExponentialBackoff) Backoff(attempt int) time.Duration {
	if attempt <= 1 || b.Base <= 0 {
		return 0
	}
	d := float64(b.Base)
	for i := 2; i < attempt; i++ {
		d *= b.Factor
		if d >= float64(b.Max) {
			return b.Max
		}
	}
	if d > float64(b.Max) {
		return b.Max
	}
	return time.Duration(d)
}

// ConstantBackoff always waits the same duration.
type ConstantBackoff time.Duration

func (b ConstantBackoff) Backoff(int) time.Duration { return time.Duration(b) }

var (
	// ErrNoExecutor is returned by New when Config.Executor is nil.
	ErrNoExecutor = errors.New("queue: executor is required")
	// ErrBadConfig is returned for invalid scheduler configuration.
	ErrBadConfig = errors.New("queue: invalid configuration")
	// ErrNotFound is returned by Cancel/Get for an unknown job id.
	ErrNotFound = errors.New("queue: job not found")
	// ErrTerminal is returned by Cancel for a job already in a terminal state.
	ErrTerminal = errors.New("queue: job already terminal")
	// ErrNoType is returned for submissions without a Type.
	ErrNoType = errors.New("queue: job type is required")
)

// Scheduler is the aging priority queue. Its zero value is not usable;
// construct with New.
type Scheduler struct {
	cfg Config

	mu      sync.Mutex
	jobs    map[string]*Job
	buckets map[int]*list.List // effective priority -> FIFO list (seq ordered)
	delayed delayedHeap
	promos  promoHeap
	nextSeq uint64
	running int
	closed  bool
	out     []Event // events buffered under mu, flushed after unlock

	timer      clock.Timer
	wake       chan struct{}
	results    chan attemptResult
	done       chan struct{}
	wg         sync.WaitGroup
	rootCtx    context.Context
	rootCancel context.CancelFunc

	// idleMu/idleAt coordinate deterministic quiescence for tests that
	// drive a manual clock (WaitSettledAt). idleAt is the scheduler-clock
	// time observed when the loop last armed its timer, updated under s.mu.
	idleMu sync.Mutex
	idleAt time.Time
	busy   int
}

// New validates cfg, applies defaults and starts the dispatch loop.
func New(cfg Config) (*Scheduler, error) {
	if cfg.Executor == nil {
		return nil, ErrNoExecutor
	}
	if cfg.Clock == nil {
		cfg.Clock = clock.Wall{}
	}
	if cfg.Sink == nil {
		cfg.Sink = NewMemorySink(0)
	}
	if cfg.MinPriority == 0 && cfg.MaxPriority == 0 {
		cfg.MinPriority, cfg.MaxPriority = 0, 9
	}
	if cfg.MinPriority > cfg.MaxPriority {
		return nil, fmt.Errorf("%w: MinPriority %d > MaxPriority %d", ErrBadConfig, cfg.MinPriority, cfg.MaxPriority)
	}
	if cfg.AgeInterval <= 0 {
		cfg.AgeInterval = time.Second
	}
	if cfg.Concurrency <= 0 {
		cfg.Concurrency = 1
	}
	if cfg.Backoff == nil {
		cfg.Backoff = ExponentialBackoff{Base: 100 * time.Millisecond, Factor: 2, Max: 5 * time.Second}
	}
	if cfg.DefaultMaxAttempts <= 0 {
		cfg.DefaultMaxAttempts = 3
	}
	if cfg.IDGenerator == nil {
		cfg.IDGenerator = randomID
	}

	rootCtx, rootCancel := context.WithCancel(context.Background())
	s := &Scheduler{
		cfg:        cfg,
		jobs:       make(map[string]*Job),
		buckets:    make(map[int]*list.List),
		wake:       make(chan struct{}, 1),
		results:    make(chan attemptResult, cfg.Concurrency),
		done:       make(chan struct{}),
		rootCtx:    rootCtx,
		rootCancel: rootCancel,
	}
	heap.Init(&s.delayed)
	heap.Init(&s.promos)
	s.timer = cfg.Clock.NewTimer(time.Hour)
	go s.loop()
	return s, nil
}

func randomID() (string, error) {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(b[:]), nil
}

// Sink returns the event sink (useful when the scheduler created its own
// MemorySink).
func (s *Scheduler) Sink() Sink { return s.cfg.Sink }

// Submit enqueues a job and returns its stored initial state.
func (s *Scheduler) Submit(req Submit) (*Job, error) {
	if req.Type == "" {
		return nil, ErrNoType
	}
	if req.Priority < s.cfg.MinPriority || req.Priority > s.cfg.MaxPriority {
		return nil, fmt.Errorf("%w: priority %d outside [%d,%d]", ErrBadConfig, req.Priority, s.cfg.MinPriority, s.cfg.MaxPriority)
	}
	maxP := req.MaxPriority
	if maxP == 0 {
		maxP = s.cfg.MaxPriority
	}
	if maxP < req.Priority || maxP > s.cfg.MaxPriority {
		return nil, fmt.Errorf("%w: maxPriority %d outside [%d,%d]", ErrBadConfig, maxP, req.Priority, s.cfg.MaxPriority)
	}
	maxAttempts := req.MaxAttempts
	if maxAttempts <= 0 {
		maxAttempts = s.cfg.DefaultMaxAttempts
	}
	id := req.ID
	if id == "" {
		var err error
		id, err = s.cfg.IDGenerator()
		if err != nil {
			return nil, fmt.Errorf("queue: id generation: %w", err)
		}
	}

	now := s.cfg.Clock.Now()
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil, errors.New("queue: scheduler closed")
	}
	if _, exists := s.jobs[id]; exists {
		s.mu.Unlock()
		return nil, fmt.Errorf("%w: duplicate id %q", ErrBadConfig, id)
	}
	s.nextSeq++
	j := &Job{
		ID:            id,
		Type:          req.Type,
		Payload:       req.Payload,
		BasePriority:  req.Priority,
		MaxPriority:   maxP,
		Status:        Queued,
		MaxAttempts:   maxAttempts,
		EnqueuedAt:    now,
		EffectivePrio: req.Priority,
		seq:           s.nextSeq,
		waitSince:     now,
		gen:           1,
	}
	s.jobs[id] = j
	if req.Delay > 0 {
		j.availableAt = now.Add(req.Delay.Std())
		heap.Push(&s.delayed, delayedItem{j: j, at: j.availableAt, gen: j.gen})
	} else {
		s.insertBucketLocked(j)
	}
	s.schedulePromoLocked(j, now)
	s.emitLocked(EventSubmitted, j.ID, map[string]any{
		"priority":          j.BasePriority,
		"maxPriority":       j.MaxPriority,
		"effectivePriority": j.BasePriority + j.level,
		"delay":             req.Delay.Std().String(),
		"maxAttempts":       j.MaxAttempts,
	})
	s.mu.Unlock()
	s.flush()
	s.notify()
	return s.Get(id)
}

// Cancel cancels a queued job or requests cancellation of a running one.
// It is idempotent in effect: canceling an unknown or already-terminal id
// returns ErrNotFound/ErrTerminal and emits a cancel_rejected event instead
// of a second canceled event.
func (s *Scheduler) Cancel(id string) error {
	s.mu.Lock()
	j, ok := s.jobs[id]
	if !ok {
		s.emitLocked(EventCancelRejected, id, map[string]any{"reason": "unknown"})
		s.mu.Unlock()
		s.flush()
		return ErrNotFound
	}
	if j.Status.IsTerminal() {
		s.emitLocked(EventCancelRejected, id, map[string]any{"reason": "terminal", "status": string(j.Status)})
		s.mu.Unlock()
		s.flush()
		return fmt.Errorf("%w: %s", ErrTerminal, j.Status)
	}
	now := s.cfg.Clock.Now()
	j.Status = Canceled
	j.FinishedAt = &now
	j.gen++ // invalidate pending delay/promotion entries
	if j.cancel != nil {
		j.cancel() // running attempt: interrupt it
	} else if j.elem != nil {
		s.removeBucketLocked(j)
	}
	// delayed/promotion heap entries are dropped lazily via gen.
	s.emitLocked(EventCanceled, j.ID, map[string]any{
		"effectivePriority": j.BasePriority + j.level,
		"attempts":          j.Attempts,
	})
	s.mu.Unlock()
	s.flush()
	s.notify()
	return nil
}

// Get returns a snapshot copy of a job, or ErrNotFound.
func (s *Scheduler) Get(id string) (*Job, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	j, ok := s.jobs[id]
	if !ok {
		return nil, ErrNotFound
	}
	return s.snapshotLocked(j), nil
}

// List returns snapshot copies of all known jobs (including terminal ones).
func (s *Scheduler) List() []*Job {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]*Job, 0, len(s.jobs))
	for _, j := range s.jobs {
		out = append(out, s.snapshotLocked(j))
	}
	return out
}

// Stats is a queue-depth snapshot.
type Stats struct {
	Running       int         `json:"running"`
	Runnable      int         `json:"runnable"`
	Delayed       int         `json:"delayed"`
	ByLevel       map[int]int `json:"byEffectivePriority"`
	RetainedTotal int         `json:"retainedTotal"`
}

// Stats returns current queue depths.
func (s *Scheduler) Stats() Stats {
	s.mu.Lock()
	defer s.mu.Unlock()
	st := Stats{Running: s.running, ByLevel: map[int]int{}}
	for lvl, l := range s.buckets {
		if l.Len() > 0 {
			st.ByLevel[lvl] = l.Len()
			st.Runnable += l.Len()
		}
	}
	for _, d := range s.delayed {
		if d.j.Status == Queued && d.gen == d.j.gen && !d.j.availableAt.IsZero() {
			st.Delayed++
		}
	}
	st.RetainedTotal = len(s.jobs)
	return st
}

// Close stops the loop and cancels in-flight attempts. Queued jobs remain in
// the in-memory queue but will never be dispatched afterwards; callers may
// still Get them.
func (s *Scheduler) Close() error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	now := s.cfg.Clock.Now()
	for _, j := range s.jobs {
		if j.Status == Running && j.cancel != nil {
			j.Status = Canceled
			j.FinishedAt = &now
			j.LastError = "scheduler shut down"
			j.cancel()
		}
	}
	s.rootCancel()
	if s.timer != nil {
		s.timer.Stop()
	}
	s.mu.Unlock()
	s.flush()
	// Wake the loop once if it is parked; rootCtx cancellation plus the
	// top-of-pass closed check terminate it. Wait for BOTH the attempt
	// goroutines and the loop itself to fully stop before returning.
	s.notify()
	s.wg.Wait()
	<-s.done
	return nil
}

// ---------------------------------------------------------------- internals

type attemptResult struct {
	j       *Job
	attempt int
	err     error
}

func (s *Scheduler) loop() {
	defer close(s.done)
	for {
		s.pump()

		s.mu.Lock()
		if s.closed {
			s.mu.Unlock()
			return
		}
		wake := s.nextWakeLocked()
		if wake.IsZero() {
			// Nothing scheduled: arm far in the future.
			wake = s.cfg.Clock.Now().Add(time.Hour)
		}
		// Absolute deadline + atomic re-arm: the clock implementation decides
		// due-ness under its own lock, so a manual-clock jump can never fall
		// between the deadline calculation and timer installation.
		armAt := s.cfg.Clock.Now()
		s.timer = s.cfg.Clock.ArmTimer(s.timer, wake)
		// Record park time under s.mu, atomically with the arm. The
		// manual-clock test sync helper WaitSettledAt reads it.
		s.idleMu.Lock()
		s.idleAt = armAt
		s.idleMu.Unlock()
		s.mu.Unlock()

		select {
		case <-s.rootCtx.Done():
			return
		case <-s.wake:
		case <-s.timer.C():
		case res := <-s.results:
			s.finishAttempt(res)
		}
	}
}

// settledLocked reports whether the scheduler has no half-finished work: no
// pump/finish in progress and no attempt result waiting to be consumed. An
// attempt blocked inside user executor code is allowed (that is normal
// running state and independent of the clock). Caller holds idleMu.
func (s *Scheduler) settledLocked() bool {
	return s.busy == 0 && len(s.results) == 0
}

// WaitSettledAt blocks until the scheduler is settled relative to a
// manual-clock target. It succeeds when no processing is in flight AND at
// least one of:
//   - the loop has parked at a clock time >= target (it definitely processed
//     everything due up to target and re-read the clock), or
//   - there is no internal promotion/delay timer due at/before target, so the
//     loop staying parked on an earlier-armed timer is correct.
//
// The second disjunct handles a jump that fires no timer: the loop need not
// wake, but there must provably be no unprocessed due work.
func (s *Scheduler) WaitSettledAt(target time.Time, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for {
		if time.Now().After(deadline) {
			return false
		}
		s.idleMu.Lock()
		idle := s.settledLocked()
		parkedAtTarget := !s.idleAt.Before(target)
		s.idleMu.Unlock()
		if !idle {
			time.Sleep(200 * time.Microsecond)
			continue
		}
		if parkedAtTarget {
			return true
		}
		// Parked earlier: only acceptable if no internal timer is due.
		s.mu.Lock()
		var due time.Time
		if len(s.delayed) > 0 {
			due = s.delayed[0].at
		}
		if len(s.promos) > 0 && (due.IsZero() || s.promos[0].at.Before(due)) {
			due = s.promos[0].at
		}
		s.mu.Unlock()
		if due.After(target) {
			return true
		}
		time.Sleep(200 * time.Microsecond)
	}
}

func (s *Scheduler) pump() {
	s.beginBusy()
	defer s.endBusy()
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	now := s.cfg.Clock.Now()
	s.processDueLocked(now)
	starting := s.dispatchLocked(now)
	s.mu.Unlock()
	s.flush()
	for _, st := range starting {
		s.wg.Add(1)
		go s.runAttempt(st.j, st.ctx, st.attempt)
	}
}

type startJob struct {
	j       *Job
	ctx     context.Context
	attempt int
}

func (s *Scheduler) notify() {
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

func (s *Scheduler) beginBusy() {
	s.idleMu.Lock()
	s.busy++
	s.idleMu.Unlock()
}

func (s *Scheduler) endBusy() {
	s.idleMu.Lock()
	s.busy--
	if s.busy == 0 {
	}
	s.idleMu.Unlock()
}

func (s *Scheduler) emitLocked(t EventType, id string, detail map[string]any) {
	s.out = append(s.out, Event{Time: s.cfg.Clock.Now(), Type: t, JobID: id, Detail: detail})
}

// flush emits events buffered under mu in order, outside the lock.
func (s *Scheduler) flush() {
	s.mu.Lock()
	out := s.out
	s.out = nil
	s.mu.Unlock()
	for _, e := range out {
		s.cfg.Sink.Emit(e)
	}
}

// processDueLocked releases delayed jobs whose time has come and applies
// every due aging promotion, cascading promotions across large clock jumps.
func (s *Scheduler) processDueLocked(now time.Time) {
	for len(s.delayed) > 0 && !s.delayed[0].at.After(now) {
		it := heap.Pop(&s.delayed).(delayedItem)
		j := it.j
		if it.gen != j.gen || j.Status != Queued {
			continue
		}
		j.availableAt = time.Time{}
		s.insertBucketLocked(j)
	}
	for len(s.promos) > 0 && !s.promos[0].at.After(now) {
		it := heap.Pop(&s.promos).(promoItem)
		j := it.j
		if it.gen != j.gen || j.Status != Queued {
			continue
		}
		s.applyPromotionLocked(j, now)
	}
}

// applyPromotionLocked raises j's level while accumulated waiting justifies
// it (so a clock jump cascades), then schedules the next level deadline.
func (s *Scheduler) applyPromotionLocked(j *Job, now time.Time) {
	waited := j.waited
	if !j.waitSince.IsZero() {
		waited += now.Sub(j.waitSince)
	}
	maxLvl := j.MaxPriority - j.BasePriority
	inBucket := j.elem != nil // currently in a runnable bucket (not delayed)?
	for j.level < maxLvl && waited >= time.Duration(j.level+1)*s.cfg.AgeInterval {
		from := j.BasePriority + j.level
		if inBucket {
			// Detach from the OLD bucket BEFORE bumping level; the element
			// still lives at the old key. Do NOT rely on j.elem afterward:
			// removal clears it, but the job must be re-inserted at the new
			// key regardless (matters when a delay release and a promotion
			// happen in the same processing pass).
			s.removeFromBucketLocked(j, from)
		}
		j.level++
		if inBucket {
			s.insertBucketLocked(j)
		}
		s.emitLocked(EventPromoted, j.ID, map[string]any{
			"from": from, "to": j.BasePriority + j.level,
		})
	}
	if j.level < maxLvl {
		s.schedulePromoLocked(j, now)
	}
}

// schedulePromoLocked pushes the next aging deadline for j.
func (s *Scheduler) schedulePromoLocked(j *Job, now time.Time) {
	maxLvl := j.MaxPriority - j.BasePriority
	if j.level >= maxLvl {
		return
	}
	target := time.Duration(j.level+1) * s.cfg.AgeInterval
	at := j.waitSince.Add(target - j.waited)
	if !at.After(now) {
		// Already eligible (e.g. requeue with retained age): a due entry is
		// picked up in the same processDueLocked pass.
		at = now
	}
	heap.Push(&s.promos, promoItem{j: j, at: at, gen: j.gen, seq: j.seq})
}

// dispatchLocked returns every job a free worker can start now.
func (s *Scheduler) dispatchLocked(now time.Time) []startJob {
	var starts []startJob
	for s.running < s.cfg.Concurrency {
		j := s.popHighestLocked()
		if j == nil {
			break
		}
		s.running++
		j.Status = Running
		if !j.waitSince.IsZero() {
			j.waited += now.Sub(j.waitSince)
			j.waitSince = time.Time{}
		}
		j.runSince = now
		j.gen++
		startedAt := now
		j.StartedAt = &startedAt
		attempt := j.Attempts + 1
		j.Attempts = attempt
		ctx, cancel := context.WithCancel(s.rootCtx)
		j.cancel = cancel
		s.emitLocked(EventStarted, j.ID, map[string]any{
			"attempt":           attempt,
			"effectivePriority": j.BasePriority + j.level,
			"waited":            j.waited.String(),
		})
		starts = append(starts, startJob{j: j, ctx: ctx, attempt: attempt})
	}
	return starts
}

func (s *Scheduler) runAttempt(j *Job, ctx context.Context, attempt int) {
	defer s.wg.Done()
	err := s.cfg.Executor.Execute(ctx, j)
	s.results <- attemptResult{j: j, attempt: attempt, err: err}
	s.notify()
}

func (s *Scheduler) finishAttempt(res attemptResult) {
	s.beginBusy()
	defer s.endBusy()
	s.mu.Lock()
	j := res.j
	if j.Status != Running {
		// Canceled (or otherwise moved) while executing.
		j.cancel = nil
		s.running--
		s.mu.Unlock()
		return
	}
	now := s.cfg.Clock.Now()
	runTime := now.Sub(j.runSince)
	j.cancel = nil
	s.running--

	if res.err == nil {
		j.Status = Succeeded
		j.FinishedAt = &now
		j.WaitTime = Duration(j.waited)
		j.RunTime = Duration(runTime)
		j.EffectivePrio = j.BasePriority + j.level
		s.emitLocked(EventSucceeded, j.ID, map[string]any{
			"attempts": j.Attempts, "waitTime": j.waited.String(), "runTime": runTime.String(),
		})
		s.mu.Unlock()
		s.flush()
		s.notify() // a slot freed: pump may dispatch more
		return
	}

	j.LastError = res.err.Error()
	fatal := IsFatal(res.err)
	if fatal || j.Attempts >= j.MaxAttempts {
		j.Status = Failed
		j.FinishedAt = &now
		j.WaitTime = Duration(j.waited)
		j.RunTime = Duration(runTime)
		s.emitLocked(EventFailed, j.ID, map[string]any{
			"attempts": j.Attempts,
			"error":    j.LastError,
			"fatal":    fatal,
			"runTime":  runTime.String(),
		})
		s.mu.Unlock()
		s.flush()
		s.notify()
		return
	}

	// Retry: age (waited) and birth sequence (seq) are deliberately kept.
	backoff := s.cfg.Backoff.Backoff(j.Attempts + 1)
	j.Status = Queued
	j.availableAt = now.Add(backoff)
	j.waitSince = now
	j.gen++
	heap.Push(&s.delayed, delayedItem{j: j, at: j.availableAt, gen: j.gen})
	// Reconcile level against retained age BEFORE recording the event so its
	// effectivePriority reflects the job's accumulated wait, not a stale
	// pre-execution value. Aging then continues through the backoff.
	s.applyPromotionLocked(j, now)
	s.emitLocked(EventRetrying, j.ID, map[string]any{
		"attempt":           j.Attempts,
		"nextAttempt":       j.Attempts + 1,
		"error":             j.LastError,
		"backoff":           backoff.String(),
		"effectivePriority": j.BasePriority + j.level,
		"age":               j.waited.String(),
	})
	s.mu.Unlock()
	s.flush()
	s.notify()
}

func (s *Scheduler) nextWakeLocked() time.Time {
	var t time.Time
	if len(s.delayed) > 0 {
		t = s.delayed[0].at
	}
	if len(s.promos) > 0 && (t.IsZero() || s.promos[0].at.Before(t)) {
		t = s.promos[0].at
	}
	return t
}

// insertBucketLocked places j into its effective-priority bucket keeping
// FIFO (birth seq) order.
func (s *Scheduler) insertBucketLocked(j *Job) {
	key := j.BasePriority + j.level
	l, ok := s.buckets[key]
	if !ok {
		l = list.New()
		s.buckets[key] = l
	}
	// Walk backwards from the newest entry; promotions usually land near the
	// back, but a retained-age job can slot in anywhere.
	var mark *list.Element
	for e := l.Back(); e != nil; e = e.Prev() {
		if e.Value.(*Job).seq < j.seq {
			mark = e
			break
		}
	}
	if mark == nil {
		j.elem = l.PushFront(j)
	} else {
		j.elem = l.InsertAfter(j, mark)
	}
}

func (s *Scheduler) removeBucketLocked(j *Job) {
	s.removeFromBucketLocked(j, j.BasePriority+j.level)
}

// removeFromBucketLocked detaches j's list element from the bucket keyed by
// effective priority key. The explicit key matters during promotion, where
// the element still lives at the OLD key although j.level is about to change.
func (s *Scheduler) removeFromBucketLocked(j *Job, key int) {
	if j.elem == nil {
		return
	}
	if l, ok := s.buckets[key]; ok {
		l.Remove(j.elem)
	}
	j.elem = nil
}

// popHighestLocked removes and returns the head of the highest non-empty
// effective-priority bucket, or nil. Within a bucket the head is the oldest
// birth sequence (FIFO).
func (s *Scheduler) popHighestLocked() *Job {
	for p := s.cfg.MaxPriority; p >= s.cfg.MinPriority; p-- {
		l := s.buckets[p]
		if l == nil || l.Len() == 0 {
			continue
		}
		front := l.Front()
		j := l.Remove(front).(*Job)
		j.elem = nil
		return j
	}
	return nil
}

func (s *Scheduler) snapshotLocked(j *Job) *Job {
	cp := *j
	if j.Payload != nil {
		cp.Payload = append([]byte(nil), j.Payload...)
	}
	cp.cancel = nil
	cp.elem = nil
	now := s.cfg.Clock.Now()
	switch j.Status {
	case Queued:
		waited := j.waited
		if !j.waitSince.IsZero() {
			waited += now.Sub(j.waitSince)
		}
		cp.WaitTime = Duration(waited)
	case Running:
		cp.RunTime = Duration(now.Sub(j.runSince))
	}
	cp.EffectivePrio = j.BasePriority + j.level
	return &cp
}

// ---------------------------------------------------------------- heaps

type delayedItem struct {
	j   *Job
	at  time.Time
	gen int
}

type delayedHeap []delayedItem

func (h delayedHeap) Len() int           { return len(h) }
func (h delayedHeap) Less(i, j int) bool { return h[i].at.Before(h[j].at) }
func (h delayedHeap) Swap(i, j int)      { h[i], h[j] = h[j], h[i] }
func (h *delayedHeap) Push(x any)        { *h = append(*h, x.(delayedItem)) }
func (h *delayedHeap) Pop() any {
	old := *h
	n := len(old)
	it := old[n-1]
	*h = old[:n-1]
	return it
}

type promoItem struct {
	j   *Job
	at  time.Time
	gen int
	seq uint64
}

type promoHeap []promoItem

func (h promoHeap) Len() int { return len(h) }
func (h promoHeap) Less(i, j int) bool {
	if h[i].at.Equal(h[j].at) {
		return h[i].seq < h[j].seq
	}
	return h[i].at.Before(h[j].at)
}
func (h promoHeap) Swap(i, j int) { h[i], h[j] = h[j], h[i] }
func (h *promoHeap) Push(x any)   { *h = append(*h, x.(promoItem)) }
func (h *promoHeap) Pop() any {
	old := *h
	n := len(old)
	it := old[n-1]
	*h = old[:n-1]
	return it
}
