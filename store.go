// Package main implements a persistent task queue with an attempt-numbered
// state machine. The point of the exercise is the cancel/complete race:
// exactly one of them may win, old attempts must never be able to commit a
// result, and a restart must never move a task backwards.
//
// # Linearization points
//
// Every mutating operation (Claim, Heartbeat, Complete, Fail, Cancel,
// Sweep, Retry) is a single critical section guarded by Store.mu. The
// operation validates preconditions, mutates the in-memory task, appends a
// full snapshot to the WAL and fsyncs it — all before releasing the lock.
// Therefore:
//
//   - Cancel vs Complete: whichever acquires the mutex first wins. The loser
//     observes the terminal state and is rejected with ErrTerminal (HTTP 409).
//     There is no window in which both can succeed.
//   - Complete vs timeout (Sweep): if the sweep runs first the attempt is
//     abandoned; a later Complete for that attempt fails with ErrStaleAttempt.
//     If Complete runs first the task is terminal and the sweep skips it.
//   - Durability: a success is returned to the caller only after the WAL
//     append+fsync, so any acknowledged state survives a crash and is
//     replayed on restart. Version is monotonic, so replay can never move a
//     task backwards.
package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
	"sync"
	"time"
)

// State is the task lifecycle state.
type State string

const (
	StatePending   State = "PENDING"   // waiting to be claimed
	StateRunning   State = "RUNNING"   // claimed by a worker, lease held
	StateSucceeded State = "SUCCEEDED" // terminal: a result was committed
	StateFailed    State = "FAILED"    // terminal: attempts exhausted
	StateCancelled State = "CANCELLED" // terminal: cancelled by a client
)

// Terminal reports whether s is a terminal state. Terminal states never
// change again; this is the invariant the race tests check.
func (s State) Terminal() bool {
	return s == StateSucceeded || s == StateFailed || s == StateCancelled
}

// Task is one unit of work. Attempt is monotonically increasing for the
// whole life of the task and is never reset, even by Retry: a result is
// only acceptable from the current attempt, so a monotonic attempt number is
// what makes stale-worker rejection possible.
type Task struct {
	ID          string    `json:"id"`
	State       State     `json:"state"`
	Attempt     int       `json:"attempt"` // 0 while never claimed
	Worker      string    `json:"worker,omitempty"`
	LeaseUntil  time.Time `json:"lease_until,omitempty"`
	Payload     string    `json:"payload,omitempty"`
	Result      string    `json:"result,omitempty"`
	MaxAttempts int       `json:"max_attempts"`
	Version     int64     `json:"version"` // monotonic, bumped on every transition
	UpdatedAt   time.Time `json:"updated_at"`
}

// Error values mapped to HTTP status codes by the handlers.
var (
	ErrNotFound       = errors.New("task not found")
	ErrTerminal       = errors.New("task is in a terminal state")
	ErrNotRunning     = errors.New("task is not running")
	ErrStaleAttempt   = errors.New("attempt does not match the current attempt")
	ErrNotClaimable   = errors.New("task is not claimable")
	ErrNotRetryable   = errors.New("task is not retryable")
	ErrWorkerMismatch = errors.New("worker does not hold the current attempt")
)

const defaultLease = 30 * time.Second

// Store is the task store. All state transitions happen under mu; that
// mutex is the single linearization point of the system.
type Store struct {
	mu       sync.Mutex
	tasks    map[string]*Task
	wal      *os.File
	enc      *json.Encoder
	now      func() time.Time // injectable clock for deterministic tests
	leaseDur time.Duration
}

// OpenStore opens (or creates) the store backed by the WAL at walPath and
// replays it. Replay applies snapshots in order and, because Version is
// monotonic, never regresses a task.
func OpenStore(walPath string) (*Store, error) {
	s := &Store{
		tasks:    make(map[string]*Task),
		now:      time.Now,
		leaseDur: defaultLease,
	}
	if _, err := os.Stat(walPath); err == nil {
		if err := s.replay(walPath); err != nil {
			return nil, fmt.Errorf("replay WAL: %w", err)
		}
	}
	f, err := os.OpenFile(walPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, err
	}
	s.wal = f
	s.enc = json.NewEncoder(f)
	return s, nil
}

func (s *Store) replay(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 1<<20), 1<<20)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var t Task
		if err := json.Unmarshal(line, &t); err != nil {
			return fmt.Errorf("corrupt WAL record: %w", err)
		}
		cur, ok := s.tasks[t.ID]
		if !ok || t.Version >= cur.Version {
			cp := t
			s.tasks[t.ID] = &cp
		}
	}
	return sc.Err()
}

// Close flushes and closes the WAL.
func (s *Store) Close() error { return s.wal.Close() }

// persist appends a full snapshot of t to the WAL and fsyncs. Must be
// called with mu held, before the success is returned to the caller.
func (s *Store) persist(t *Task) error {
	if err := s.enc.Encode(t); err != nil {
		return err
	}
	return s.wal.Sync()
}

// Submit creates a new task in PENDING state.
func (s *Store) Submit(id, payload string, maxAttempts int) (*Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.tasks[id]; ok {
		return nil, fmt.Errorf("task %q already exists", id)
	}
	if maxAttempts <= 0 {
		maxAttempts = 3
	}
	t := &Task{
		ID:          id,
		State:       StatePending,
		Payload:     payload,
		MaxAttempts: maxAttempts,
		Version:     1,
		UpdatedAt:   s.now(),
	}
	if err := s.persist(t); err != nil {
		return nil, err
	}
	s.tasks[id] = t
	return t, nil
}

// Get returns a copy of the task. A RUNNING task whose lease has expired is
// reported (and persisted) as PENDING — the lazy half of the timeout path.
func (s *Store) Get(id string) (*Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	t, ok := s.tasks[id]
	if !ok {
		return nil, ErrNotFound
	}
	if err := s.expireLocked(t, s.now()); err != nil {
		return nil, err
	}
	cp := *t
	return &cp, nil
}

// List returns copies of all tasks, applying lazy expiry first.
func (s *Store) List() ([]*Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	out := make([]*Task, 0, len(s.tasks))
	for _, t := range s.tasks {
		if err := s.expireLocked(t, now); err != nil {
			return nil, err
		}
		cp := *t
		out = append(out, &cp)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out, nil
}

// expireLocked abandons a RUNNING attempt whose lease has expired, moving
// the task back to PENDING. The attempt number is kept: the expired attempt
// is now stale and any result from it will be rejected.
func (s *Store) expireLocked(t *Task, now time.Time) error {
	if t.State != StateRunning || now.Before(t.LeaseUntil) {
		return nil
	}
	t.State = StatePending
	t.Worker = ""
	t.LeaseUntil = time.Time{}
	t.Version++
	t.UpdatedAt = now
	return s.persist(t)
}

// Sweep applies lease expiry to every task as of now. It is the explicit
// timeout path (a background ticker calls it in main; tests call it with a
// controlled clock).
func (s *Store) Sweep(now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, t := range s.tasks {
		if err := s.expireLocked(t, now); err != nil {
			return err
		}
	}
	return nil
}

// Claim moves a PENDING task to RUNNING and bumps the attempt number.
func (s *Store) Claim(id, worker string) (*Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	t, ok := s.tasks[id]
	if !ok {
		return nil, ErrNotFound
	}
	if err := s.expireLocked(t, s.now()); err != nil {
		return nil, err
	}
	if t.State.Terminal() {
		return nil, ErrTerminal
	}
	if t.State != StatePending {
		return nil, ErrNotClaimable
	}
	now := s.now()
	t.State = StateRunning
	t.Attempt++
	t.Worker = worker
	t.LeaseUntil = now.Add(s.leaseDur)
	t.Version++
	t.UpdatedAt = now
	if err := s.persist(t); err != nil {
		return nil, err
	}
	cp := *t
	return &cp, nil
}

// Heartbeat extends the lease of the current attempt.
func (s *Store) Heartbeat(id, worker string, attempt int) (*Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	t, err := s.checkAttemptLocked(id, worker, attempt)
	if err != nil {
		return nil, err
	}
	now := s.now()
	t.LeaseUntil = now.Add(s.leaseDur)
	t.Version++
	t.UpdatedAt = now
	if err := s.persist(t); err != nil {
		return nil, err
	}
	cp := *t
	return &cp, nil
}

// Complete commits a result. Linearization point vs Cancel: the mutex.
// Only the current attempt of a RUNNING task may complete; anything else is
// rejected, so a stale worker can never overwrite a terminal state.
func (s *Store) Complete(id, worker string, attempt int, result string) (*Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	t, err := s.checkAttemptLocked(id, worker, attempt)
	if err != nil {
		return nil, err
	}
	now := s.now()
	t.State = StateSucceeded
	t.Result = result
	t.Worker = ""
	t.LeaseUntil = time.Time{}
	t.Version++
	t.UpdatedAt = now
	if err := s.persist(t); err != nil {
		return nil, err
	}
	cp := *t
	return &cp, nil
}

// Fail abandons the current attempt. The task goes back to PENDING for a
// retry, or to FAILED if the attempt budget is exhausted.
func (s *Store) Fail(id, worker string, attempt int) (*Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	t, err := s.checkAttemptLocked(id, worker, attempt)
	if err != nil {
		return nil, err
	}
	now := s.now()
	if t.Attempt >= t.MaxAttempts {
		t.State = StateFailed
	} else {
		t.State = StatePending
	}
	t.Worker = ""
	t.LeaseUntil = time.Time{}
	t.Version++
	t.UpdatedAt = now
	if err := s.persist(t); err != nil {
		return nil, err
	}
	cp := *t
	return &cp, nil
}

// Cancel moves any non-terminal task to CANCELLED. Cancelling an already
// CANCELLED task is idempotent (returns the task); cancelling a SUCCEEDED
// or FAILED task is a conflict — the race was lost.
func (s *Store) Cancel(id string) (*Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	t, ok := s.tasks[id]
	if !ok {
		return nil, ErrNotFound
	}
	if t.State == StateCancelled {
		cp := *t
		return &cp, nil
	}
	if t.State.Terminal() {
		return nil, ErrTerminal
	}
	now := s.now()
	t.State = StateCancelled
	t.Worker = ""
	t.LeaseUntil = time.Time{}
	t.Version++
	t.UpdatedAt = now
	if err := s.persist(t); err != nil {
		return nil, err
	}
	cp := *t
	return &cp, nil
}

// Retry re-opens a FAILED task (manual retry). Attempt is not reset, so
// results from any earlier attempt stay stale forever.
func (s *Store) Retry(id string) (*Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	t, ok := s.tasks[id]
	if !ok {
		return nil, ErrNotFound
	}
	if t.State != StateFailed {
		return nil, ErrNotRetryable
	}
	now := s.now()
	t.State = StatePending
	t.Version++
	t.UpdatedAt = now
	if err := s.persist(t); err != nil {
		return nil, err
	}
	cp := *t
	return &cp, nil
}

// checkAttemptLocked validates the common precondition of Heartbeat,
// Complete and Fail: the task exists, is RUNNING, and the caller presents
// the current attempt number and the worker that holds it.
func (s *Store) checkAttemptLocked(id, worker string, attempt int) (*Task, error) {
	t, ok := s.tasks[id]
	if !ok {
		return nil, ErrNotFound
	}
	if t.State.Terminal() {
		return nil, ErrTerminal
	}
	if t.State != StateRunning {
		return nil, ErrNotRunning
	}
	if attempt != t.Attempt {
		return nil, ErrStaleAttempt
	}
	if worker != t.Worker {
		return nil, ErrWorkerMismatch
	}
	return t, nil
}
