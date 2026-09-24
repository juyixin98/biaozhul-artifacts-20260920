// Package machine implements the in-memory, single-writer task state machine.
//
// It has NO dependency on HTTP or on durable storage. Every state transition
// is expressed as a Cmd applied through Machine.Step. Each accepted
// transition emits one or more Events (full task snapshots keyed by a
// monotonically increasing sequence number). A second Machine can reproduce
// the exact same state by replaying those events with Restore, which is what
// the persistence layer does after a restart.
//
// Concurrency: Machine itself is NOT goroutine-safe. The store package
// serializes all callers behind one mutex; that lock, together with the WAL
// fsync, defines the linearization points (see README).
package machine

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"
)

// State is the task lifecycle state.
type State string

const (
	// Pending: queued, not currently owned by any worker.
	Pending State = "PENDING"
	// Running: leased by exactly one worker for Attempt.
	Running State = "RUNNING"
	// Completed: terminal; a result was committed by the lease owner.
	Completed State = "COMPLETED"
	// Cancelled: terminal; cancel won the race (or the task was never claimed).
	Cancelled State = "CANCELLED"
)

// Valid reports whether s is a known state.
func (s State) Valid() bool {
	switch s {
	case Pending, Running, Completed, Cancelled:
		return true
	}
	return false
}

// Terminal reports whether no further transition is possible.
func (s State) Terminal() bool { return s == Completed || s == Cancelled }

// ErrorCode is a stable, machine-readable error code returned to clients.
type ErrorCode string

const (
	CodeBadRequest   ErrorCode = "BAD_REQUEST"
	CodeNotFound     ErrorCode = "NOT_FOUND"
	CodeNoTask       ErrorCode = "NO_TASK_AVAILABLE"
	CodeInvalidState ErrorCode = "INVALID_STATE"
	CodeStaleLease   ErrorCode = "STALE_LEASE"
)

// BizError is a business-level rejection (as opposed to an I/O failure).
type BizError struct {
	HTTPStatus int
	Code       ErrorCode
	Message    string
}

func (e *BizError) Error() string { return string(e.Code) + ": " + e.Message }

func bizError(status int, code ErrorCode, format string, args ...any) *BizError {
	return &BizError{HTTPStatus: status, Code: code, Message: fmt.Sprintf(format, args...)}
}

// Task is the full persisted state of one task.
type Task struct {
	ID      string          `json:"id"`
	Payload json.RawMessage `json:"payload,omitempty"`
	State   State           `json:"state"`
	// Attempt is the dispatch number. It starts at 0 and is incremented
	// exactly when a PENDING task is claimed. Timeouts and worker-requested
	// retries move the task back to PENDING WITHOUT changing Attempt; the
	// next claim starts the next attempt.
	Attempt       int             `json:"attempt"`
	WorkerID      string          `json:"worker_id,omitempty"`
	LeaseToken    string          `json:"lease_token,omitempty"`
	LeaseDeadline time.Time       `json:"lease_deadline,omitempty"`
	Result        json.RawMessage `json:"result,omitempty"`
	// Version is the sequence number of the last event that changed this task.
	Version   int64     `json:"version"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// Event is one persisted state transition: a full post-transition task
// snapshot paired with its global sequence number.
type Event struct {
	Seq  int64 `json:"seq"`
	Task Task  `json:"task"`
}

// Kind enumerates the commands.
type Kind string

const (
	SubmitCmd    Kind = "submit"
	ClaimCmd     Kind = "claim"
	HeartbeatCmd Kind = "heartbeat"
	CompleteCmd  Kind = "complete"
	CancelCmd    Kind = "cancel"
	RetryCmd     Kind = "retry"
)

// Cmd is a state-transition request. WorkerID/LeaseToken identify the lease
// owner for Heartbeat/Complete/Retry; Now is the logical time at which the
// command linearizes.
type Cmd struct {
	Kind       Kind
	ID         string
	WorkerID   string
	LeaseToken string
	Payload    json.RawMessage // Submit: task payload; Complete: result
	Now        time.Time
}

// Config configures a Machine. The default ID/token generators use
// crypto/random; deterministic generators can be injected (tests use them).
type Config struct {
	LeaseTTL time.Duration
	NewID    func(seq int64) string
	NewToken func(seq int64, taskID string, attempt int) string
}

// Machine is the pure state machine.
type Machine struct {
	cfg   Config
	tasks map[string]*Task
	order []string // insertion order, used to make claim deterministic
	seq   int64
}

// New creates an empty machine.
func New(cfg Config) *Machine {
	if cfg.LeaseTTL <= 0 {
		cfg.LeaseTTL = 30 * time.Second
	}
	if cfg.NewID == nil {
		cfg.NewID = func(int64) string { return "task_" + randomHex(8) }
	}
	if cfg.NewToken == nil {
		cfg.NewToken = func(_ int64, _ string, _ int) string { return "lease_" + randomHex(16) }
	}
	return &Machine{cfg: cfg, tasks: map[string]*Task{}}
}

func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		// crypto/rand failing is not recoverable; surface it as a panic.
		panic(fmt.Errorf("machine: cannot read random bytes: %w", err))
	}
	return hex.EncodeToString(b)
}

// touch bumps Version/UpdatedAt and records an event for t.
func (m *Machine) touch(t *Task, now time.Time) Event {
	m.seq++
	t.Version = m.seq
	t.UpdatedAt = now
	return Event{Seq: m.seq, Task: *t}
}

// ExpireDue moves every RUNNING task whose lease has expired back to PENDING.
// It is the lazy half of timeout handling: every command calls (via Step)
// ExpireDue at the very beginning of its critical section, so a timeout is
// always linearized before the command itself. Returns one event per timed
// out task. A lease expires exactly when lease_deadline <= now.
func (m *Machine) ExpireDue(now time.Time) []Event {
	var events []Event
	for _, id := range m.order {
		t := m.tasks[id]
		if t.State != Running {
			continue
		}
		if t.LeaseDeadline.After(now) {
			continue
		}
		// Timeout. Attempt is NOT incremented: it counts dispatches, and
		// the next claim performs the next dispatch. The old lease token
		// dies here immediately, even before the task is re-claimed.
		t.State = Pending
		t.WorkerID = ""
		t.LeaseToken = ""
		t.LeaseDeadline = time.Time{}
		e := m.touch(t, now)
		events = append(events, e)
	}
	return events
}

// Step linearizes one command: first all due leases expire (at c.Now), then
// the command itself is applied. Events are returned in the order they must
// be persisted; for an accepted command the command's own event is last.
func (m *Machine) Step(c Cmd) (*Task, []Event, *BizError) {
	events := m.ExpireDue(c.Now)
	t, ev, err := m.Apply(c)
	if err != nil {
		return t, nil, err
	}
	if ev != nil {
		events = append(events, *ev)
	}
	return t, events, nil
}

// Apply executes the raw transition without expiring leases. Callers should
// normally use Step.
func (m *Machine) Apply(c Cmd) (*Task, *Event, *BizError) {
	switch c.Kind {
	case SubmitCmd:
		return m.submit(c)
	case ClaimCmd:
		return m.claim(c)
	case HeartbeatCmd, CompleteCmd, CancelCmd, RetryCmd:
		t, ok := m.tasks[c.ID]
		if !ok {
			return nil, nil, bizError(404, CodeNotFound, "task %q not found", c.ID)
		}
		switch c.Kind {
		case HeartbeatCmd:
			return m.heartbeat(t, c)
		case CompleteCmd:
			return m.complete(t, c)
		case CancelCmd:
			return m.cancel(t, c)
		case RetryCmd:
			return m.retry(t, c)
		}
	}
	return nil, nil, bizError(400, CodeBadRequest, "unknown command %q", c.Kind)
}

func (m *Machine) submit(c Cmd) (*Task, *Event, *BizError) {
	if len(c.Payload) > 0 && !json.Valid(c.Payload) {
		return nil, nil, bizError(400, CodeBadRequest, "payload is not valid JSON")
	}
	m.seq++
	id := c.ID
	if id == "" {
		id = m.cfg.NewID(m.seq)
	}
	if _, exists := m.tasks[id]; exists {
		return nil, nil, bizError(409, CodeInvalidState, "task %q already exists", id)
	}
	t := &Task{
		ID:        id,
		Payload:   append(json.RawMessage(nil), c.Payload...),
		State:     Pending,
		Version:   m.seq,
		CreatedAt: c.Now,
		UpdatedAt: c.Now,
	}
	m.tasks[id] = t
	m.order = append(m.order, id)
	e := Event{Seq: m.seq, Task: *t}
	return t, &e, nil
}

func (m *Machine) claim(c Cmd) (*Task, *Event, *BizError) {
	if c.WorkerID == "" {
		return nil, nil, bizError(400, CodeBadRequest, "worker_id is required to claim a task")
	}
	for _, id := range m.order {
		t := m.tasks[id]
		if t.State != Pending {
			continue
		}
		t.State = Running
		t.Attempt++ // the next dispatch begins
		t.WorkerID = c.WorkerID
		t.LeaseToken = m.cfg.NewToken(m.seq+1, t.ID, t.Attempt)
		t.LeaseDeadline = c.Now.Add(m.cfg.LeaseTTL)
		e := m.touch(t, c.Now)
		return t, &e, nil
	}
	return nil, nil, bizError(404, CodeNoTask, "no task available to claim")
}

// authorizeOwner checks that the caller holds the current lease. It must be
// called only after expiry has been processed and while the task is RUNNING:
// a result/heartbeat from an old attempt is rejected here because either the
// task already left RUNNING or the token differs.
func authorizeOwner(t *Task, c Cmd) *BizError {
	if t.State != Running {
		return bizError(409, CodeInvalidState,
			"task %q is %s, not RUNNING; results are only accepted from the current attempt", t.ID, t.State)
	}
	if t.WorkerID != c.WorkerID || t.LeaseToken != c.LeaseToken || c.LeaseToken == "" {
		return bizError(409, CodeStaleLease,
			"worker %q does not hold the current lease for task %q", c.WorkerID, t.ID)
	}
	return nil
}

func (m *Machine) heartbeat(t *Task, c Cmd) (*Task, *Event, *BizError) {
	if err := authorizeOwner(t, c); err != nil {
		return t, nil, err
	}
	t.LeaseDeadline = c.Now.Add(m.cfg.LeaseTTL)
	e := m.touch(t, c.Now)
	return t, &e, nil
}

func (m *Machine) complete(t *Task, c Cmd) (*Task, *Event, *BizError) {
	if err := authorizeOwner(t, c); err != nil {
		return t, nil, err
	}
	if len(c.Payload) > 0 && !json.Valid(c.Payload) {
		return nil, nil, bizError(400, CodeBadRequest, "result is not valid JSON")
	}
	// Linearization point of completion: under the store lock, the task is
	// RUNNING and the caller holds its current lease. The state flips to
	// COMPLETED atomically with the result; any cancel arriving after this
	// point is rejected.
	t.State = Completed
	t.Result = append(json.RawMessage(nil), c.Payload...)
	t.WorkerID = ""
	t.LeaseToken = ""
	t.LeaseDeadline = time.Time{}
	e := m.touch(t, c.Now)
	return t, &e, nil
}

func (m *Machine) cancel(t *Task, c Cmd) (*Task, *Event, *BizError) {
	switch t.State {
	case Pending, Running:
		// Linearization point of cancellation. The lease (if any) is killed
		// and the task becomes terminal; any complete arriving after this
		// point is rejected.
		t.State = Cancelled
		t.WorkerID = ""
		t.LeaseToken = ""
		t.LeaseDeadline = time.Time{}
		e := m.touch(t, c.Now)
		return t, &e, nil
	case Cancelled:
		// Idempotent success, no new event.
		return t, nil, nil
	case Completed:
		return t, nil, bizError(409, CodeInvalidState,
			"task %q is COMPLETED; completion won the cancel/complete race", t.ID)
	default:
		return t, nil, bizError(409, CodeInvalidState, "task %q is %s", t.ID, t.State)
	}
}

func (m *Machine) retry(t *Task, c Cmd) (*Task, *Event, *BizError) {
	switch t.State {
	case Pending:
		// Already queued for (re)dispatch: idempotent success, no event.
		return t, nil, nil
	case Running:
		// Only the current lease owner may voluntarily give the task back.
		// A different worker must not be able to cancel someone else's run.
		if t.WorkerID != c.WorkerID || t.LeaseToken != c.LeaseToken || c.LeaseToken == "" {
			return t, nil, bizError(409, CodeStaleLease,
				"worker %q does not hold the current lease for task %q", c.WorkerID, t.ID)
		}
		t.State = Pending
		t.WorkerID = ""
		t.LeaseToken = ""
		t.LeaseDeadline = time.Time{}
		e := m.touch(t, c.Now)
		return t, &e, nil
	case Completed, Cancelled:
		return t, nil, bizError(409, CodeInvalidState,
			"task %q is terminal (%s) and cannot be retried", t.ID, t.State)
	default:
		return t, nil, bizError(409, CodeInvalidState, "task %q is %s", t.ID, t.State)
	}
}

// Get returns a copy of the task, or nil if absent.
func (m *Machine) Get(id string) *Task {
	if t, ok := m.tasks[id]; ok {
		cp := *t
		return &cp
	}
	return nil
}

// Snapshot returns copies of all tasks in deterministic order.
func (m *Machine) Snapshot() []Task {
	out := make([]Task, 0, len(m.order))
	for _, id := range m.order {
		out = append(out, *m.tasks[id])
	}
	return out
}

// Seq returns the last assigned sequence number.
func (m *Machine) Seq() int64 { return m.seq }

// Restore replays one event. Events are full post-state snapshots, so restore
// is an upsert; an event with an already-seen seq is ignored.
func (m *Machine) Restore(e Event) {
	if e.Seq <= m.seq {
		return
	}
	cp := e.Task
	if _, exists := m.tasks[cp.ID]; !exists {
		m.order = append(m.order, cp.ID)
	}
	m.tasks[cp.ID] = &cp
	m.seq = e.Seq
}

// Reset is used by tests to rebuild an identical machine from a snapshot.
func (m *Machine) Reset(seq int64, tasks []Task) {
	m.tasks = make(map[string]*Task, len(tasks))
	m.order = m.order[:0]
	for i := range tasks {
		cp := tasks[i]
		m.tasks[cp.ID] = &cp
		m.order = append(m.order, cp.ID)
	}
	m.seq = seq
}
