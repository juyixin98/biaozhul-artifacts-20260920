// Package store wraps the pure machine with:
//
//   - a single RWMutex that serializes every transition,
//   - durable WAL appends (fsync) performed INSIDE the same critical section,
//   - automatic lease-expiry sweeps driven by a background ticker,
//   - snapshot compaction once the WAL grows past a threshold,
//   - replay from disk on Open, so state never regresses across restarts.
//
// Linearization points: every mutating HTTP request takes mu, computes the
// logical "now", lets the machine expire due leases, applies the command and
// fsyncs the resulting events — all while holding mu. The order in which
// competing requests acquire mu is therefore the total order in which cancel
// and complete race, and the winner is fixed before the response is sent.
package store

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"time"

	"taskq/internal/machine"
	"taskq/internal/wal"
)

// Options configures a Store. Zero-value durations select defaults; an
// explicit Now lets tests drive a deterministic clock.
type Options struct {
	LeaseTTL      time.Duration
	SweepInterval time.Duration
	CompactEvery  int64 // compact after this many WAL records (0 = 256)
	Now           func() time.Time
	NewID         func(seq int64) string
	NewToken      func(seq int64, taskID string, attempt int) string
}

// Store is the durable, thread-safe task store.
type Store struct {
	mu sync.Mutex
	m  *machine.Machine
	w  *wal.Log

	now          func() time.Time
	compactEvery int64
	walRecords   int64 // records appended since the last compaction
	failed       bool  // set after a persistence failure; fail fast afterwards
	failErr      error
}

// Open creates or reopens a store in dir, replaying snapshot+WAL.
func Open(dir string, opts Options) (*Store, error) {
	if opts.LeaseTTL <= 0 {
		opts.LeaseTTL = 30 * time.Second
	}
	if opts.CompactEvery <= 0 {
		opts.CompactEvery = 256
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}

	w, err := wal.Open(dir)
	if err != nil {
		return nil, err
	}
	snap, recs, err := w.Load()
	if err != nil {
		w.Close()
		return nil, err
	}
	m := machine.New(machine.Config{
		LeaseTTL: opts.LeaseTTL,
		NewID:    opts.NewID,
		NewToken: opts.NewToken,
	})

	walCount := int64(0)
	if snap != nil {
		var tasks []machine.Task
		for _, raw := range snap.Tasks {
			var t machine.Task
			if err := json.Unmarshal(raw, &t); err != nil {
				w.Close()
				return nil, fmt.Errorf("store: corrupt snapshot task: %w", err)
			}
			tasks = append(tasks, t)
		}
		m.Reset(snap.Seq, tasks)
	}
	for _, r := range recs {
		var e machine.Event
		if err := json.Unmarshal(r.Task, &e.Task); err != nil {
			w.Close()
			return nil, fmt.Errorf("store: corrupt wal task: %w", err)
		}
		e.Seq = r.Seq
		m.Restore(e)
		walCount++
	}

	s := &Store{
		m:            m,
		w:            w,
		now:          opts.Now,
		compactEvery: opts.CompactEvery,
		walRecords:   walCount,
	}
	return s, nil
}

// Close flushes and closes the log.
func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.w.Close()
}

// result is what step returns after the lock is released.
type result struct {
	task   *machine.Task
	events []machine.Event
}

// step linearizes one command against every other caller.
func (s *Store) step(cmd machine.Cmd) (*machine.Task, []machine.Event, *machine.BizError) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if s.failed {
		return nil, nil, &machine.BizError{HTTPStatus: 500, Code: "PERSISTENCE_FAILED",
			Message: "store is in failure mode after: " + s.failErr.Error()}
	}
	cmd.Now = s.now()
	t, events, err := s.m.Step(cmd)
	if err != nil {
		return t, nil, err
	}
	if len(events) > 0 {
		if perr := s.persist(events); perr != nil {
			return nil, nil, &machine.BizError{HTTPStatus: 500, Code: "PERSISTENCE_FAILED",
				Message: perr.Error()}
		}
	}
	return t, events, nil
}

// persist writes events to the WAL (caller holds mu). Every event is one
// fsynced record; the state and the disk write commit together under the
// same lock, so an acknowledged transition can never be lost silently.
func (s *Store) persist(events []machine.Event) error {
	for _, e := range events {
		raw, err := json.Marshal(e.Task)
		if err != nil {
			return fmt.Errorf("marshal task %s: %w", e.Task.ID, err)
		}
		if err := s.w.Append(wal.Record{Seq: e.Seq, Task: raw}); err != nil {
			// Fail fast: the in-memory transition happened but the disk write
			// did not, so we must not serve as if it had.
			s.failed = true
			s.failErr = err
			return err
		}
		s.walRecords++
	}
	if s.walRecords >= s.compactEvery {
		if err := s.compactLocked(); err != nil {
			s.failed = true
			s.failErr = err
			return err
		}
	}
	return nil
}

func (s *Store) compactLocked() error {
	tasks := s.m.Snapshot()
	raw := make([]json.RawMessage, 0, len(tasks))
	for _, t := range tasks {
		b, err := json.Marshal(t)
		if err != nil {
			return err
		}
		raw = append(raw, b)
	}
	if err := s.w.Compact(wal.Snapshot{Seq: s.m.Seq(), Tasks: raw}); err != nil {
		return err
	}
	s.walRecords = 0
	return nil
}

// --- Public API -----------------------------------------------------------

func (s *Store) Submit(payload json.RawMessage) (*machine.Task, *machine.BizError) {
	t, _, err := s.step(machine.Cmd{Kind: machine.SubmitCmd, Payload: payload})
	return t, err
}

func (s *Store) Claim(workerID string) (*machine.Task, string, *machine.BizError) {
	t, _, err := s.step(machine.Cmd{Kind: machine.ClaimCmd, WorkerID: workerID})
	if err != nil {
		return nil, "", err
	}
	return t, t.LeaseToken, nil
}

func (s *Store) Heartbeat(id, workerID, token string) (*machine.Task, *machine.BizError) {
	t, _, err := s.step(machine.Cmd{Kind: machine.HeartbeatCmd, ID: id,
		WorkerID: workerID, LeaseToken: token})
	return t, err
}

func (s *Store) Complete(id, workerID, token string, result json.RawMessage) (*machine.Task, *machine.BizError) {
	t, _, err := s.step(machine.Cmd{Kind: machine.CompleteCmd, ID: id,
		WorkerID: workerID, LeaseToken: token, Payload: result})
	return t, err
}

func (s *Store) Cancel(id string) (*machine.Task, *machine.BizError) {
	t, _, err := s.step(machine.Cmd{Kind: machine.CancelCmd, ID: id})
	return t, err
}

func (s *Store) Retry(id, workerID, token string) (*machine.Task, *machine.BizError) {
	t, _, err := s.step(machine.Cmd{Kind: machine.RetryCmd, ID: id,
		WorkerID: workerID, LeaseToken: token})
	return t, err
}

// Sweep forces one expiry pass immediately (used by the background ticker
// and by tests). Timeouts are transitions just like client requests: they
// acquire mu, expire due leases and fsync the events.
func (s *Store) Sweep() (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.failed {
		return 0, fmt.Errorf("store failed: %w", s.failErr)
	}
	events := s.m.ExpireDue(s.now())
	if len(events) == 0 {
		return 0, nil
	}
	if err := s.persist(events); err != nil {
		return 0, err
	}
	return len(events), nil
}

// Get returns a copy of a task. It runs the same lazy expiry pass as List so
// a task whose lease deadline has passed is observed as PENDING immediately.
func (s *Store) Get(id string) *machine.Task {
	s.mu.Lock()
	defer s.mu.Unlock()
	if evs := s.m.ExpireDue(s.now()); len(evs) > 0 {
		// persist() flips the store into fail-fast mode on I/O error.
		_ = s.persist(evs)
	}
	return s.m.Get(id)
}

// List returns copies of all tasks in submission order. It performs a lazy
// expiry pass first so status never appears stale to API readers (the pass
// is read-only when nothing has expired).
func (s *Store) List() ([]machine.Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.failed {
		return nil, fmt.Errorf("store failed: %w", s.failErr)
	}
	if evs := s.m.ExpireDue(s.now()); len(evs) > 0 {
		if err := s.persist(evs); err != nil {
			return nil, err
		}
	}
	return s.m.Snapshot(), nil
}

// StartSweeper launches the background timeout sweeper until ctx is done.
func (s *Store) StartSweeper(ctx context.Context, interval time.Duration) {
	if interval <= 0 {
		interval = time.Second
	}
	go func() {
		t := time.NewTicker(interval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				_, _ = s.Sweep()
			}
		}
	}()
}
