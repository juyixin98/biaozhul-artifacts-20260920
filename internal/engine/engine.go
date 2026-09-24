// Package engine implements resumable behavior tree execution: the tick
// algorithm (Sequence / Fallback / Parallel / Timeout), durable state,
// action-call dedup, subtree cancellation, and restart recovery.
//
// # Concurrency model
//
// Ticks for the same execution are serialized by a PostgreSQL advisory lock
// (hashed from the execution id), held across every transaction of the tick.
// Physical stub side effects use the saga pattern: their action_calls claim
// and outcome rows are committed in independent transactions immediately, so
// a successful non-idempotent stub is never executed twice even if the tree
// state transaction is later rolled back (tick interruption) or the process
// dies. Tree/node state itself is written in one final transaction and
// latched: restarting the process resumes by re-reading that state.
//
// # Late results
//
// A stub attempt runs under a context carrying its attempt number. Every
// completion is fenced in SQL: it is applied only when the persisted attempt
// still matches. A result arriving after cancellation, timeout, abort or
// relaunch has a stale attempt and is discarded. Results never walk the tree;
// they only update the latch row, so no late result can resume a tree.
package engine

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgtype"

	"bt/internal/model"
	"bt/internal/store"
	"bt/internal/stub"
)

// TickResult is the API-facing outcome of one tick.
type TickResult struct {
	TickID     string             `json:"tick_id"`
	Seq        int64              `json:"seq"`
	Status     string             `json:"status"` // completed | interrupted
	TreeStatus string             `json:"tree_status"`
	Nodes      map[string]string  `json:"nodes"`
	Calls      map[string]CallDTO `json:"calls"`
}

// CallDTO is one action call snapshot in API responses.
type CallDTO struct {
	CallID        string          `json:"call_id"`
	Stub          string          `json:"stub"`
	Attempt       int             `json:"attempt"`
	Status        string          `json:"status"`
	NonIdempotent bool            `json:"non_idempotent"`
	Result        json.RawMessage `json:"result,omitempty"`
	Error         string          `json:"error,omitempty"`
}

// SnapshotDTO is the full state of an execution.
type SnapshotDTO struct {
	ExecutionID string             `json:"execution_id"`
	TreeID      string             `json:"tree_id"`
	TreeVersion int                `json:"tree_version"`
	TreeHash    string             `json:"tree_hash"`
	Status      string             `json:"status"`
	Nodes       map[string]string  `json:"nodes"`
	Calls       map[string]CallDTO `json:"calls"`
	LastTick    *TickResult        `json:"last_tick,omitempty"`
}

// Options configures an Engine.
type Options struct {
	// WallClock is overridable in tests.
	Now func() time.Time
}

// Engine ties together persistence and the stub registry.
type Engine struct {
	st  *store.Store
	rg  *stub.Registry
	now func() time.Time

	// execCtx holds one live cancelable context per execution; it is the
	// parent of all in-process action attempt contexts. Abort and server
	// shutdown cancel it. On process restart it is lazily recreated; rows
	// left 'running' by the dead process are reaped first.
	execMu  sync.Mutex
	execCtx map[string]*execRuntime
}

type execRuntime struct {
	ctx    context.Context
	cancel context.CancelFunc

	// live attempts in this process: node id -> attempt handle.
	mu       sync.Mutex
	attempts map[string]*attemptHandle
}

type attemptHandle struct {
	nodeID  string
	attempt int
	cancel  context.CancelFunc
}

// New constructs an Engine.
func New(st *store.Store, rg *stub.Registry, opts Options) *Engine {
	now := opts.Now
	if now == nil {
		now = time.Now
	}
	return &Engine{st: st, rg: rg, now: now, execCtx: map[string]*execRuntime{}}
}

// Registry exposes the stub registry (for internal control endpoints).
func (e *Engine) Registry() *stub.Registry { return e.rg }

func randomID(prefix string) string {
	var b [12]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err) // crypto/rand failure is unrecoverable
	}
	return prefix + "_" + hex.EncodeToString(b[:])
}

// advisoryKey derives a stable 64-bit advisory-lock key from an execution id.
func advisoryKey(execID string) int64 {
	sum := sha256.Sum256([]byte("exec-lock:" + execID))
	return int64(binary.BigEndian.Uint64(sum[:8]))
}

// runtimeFor returns the in-process runtime for an execution, creating it.
func (e *Engine) runtimeFor(execID string) *execRuntime {
	e.execMu.Lock()
	defer e.execMu.Unlock()
	rt, ok := e.execCtx[execID]
	if !ok {
		ctx, cancel := context.WithCancel(context.Background())
		rt = &execRuntime{ctx: ctx, cancel: cancel, attempts: map[string]*attemptHandle{}}
		e.execCtx[execID] = rt
	}
	return rt
}

// tickContext is the mutable working state of one tick evaluation.
type tickContext struct {
	engine      *Engine
	execID      string
	tree        *model.Tree
	tickID      string
	seq         int64
	ctx         context.Context // canceled on tick interruption
	rt          *execRuntime
	states      map[string]*store.NodeStateRow
	calls       map[string]*store.CallRow
	pendingSets []setState
	launched    map[string]int                // action attempts started by THIS tick: node id -> attempt
	deadlines   map[string]pgtype.Timestamptz // timeout deadlines set this tick
	canceled    map[string]bool               // nodes canceled by a parent this tick
}

type setState struct {
	nodeID string
	status string
}

func (tc *tickContext) nodeStatus(id string) string {
	if tc.canceled[id] {
		return model.StatusCanceled
	}
	if r, ok := tc.states[id]; ok {
		return r.Status
	}
	return ""
}

// setNode records an in-memory node status change (written at tick commit).
// It preserves previously attached data (e.g. the timeout deadline), so
// repeated "running" updates never wipe the persisted deadline.
func (tc *tickContext) setNode(id, status string) {
	row := &store.NodeStateRow{NodeID: id, Status: status, UpdatedSeq: tc.seq}
	if old := tc.states[id]; old != nil {
		row.DeadlineAt = old.DeadlineAt
	}
	tc.states[id] = row
	tc.pendingSets = append(tc.pendingSets, setState{id, status})
}

// setDeadline records a timeout deadline for commit.
func (tc *tickContext) setDeadline(id string, t time.Time) {
	ts := pgtype.Timestamptz{Time: t.UTC(), Valid: true}
	tc.deadlines[id] = ts
	r := tc.states[id]
	if r == nil {
		r = &store.NodeStateRow{NodeID: id, Status: model.StatusRunning, UpdatedSeq: tc.seq}
		tc.states[id] = r
	}
	r.DeadlineAt = &ts
}

var (
	errInterrupted = errors.New("tick interrupted")
	errAborted     = errors.New("execution aborted")
)

// aborted reports whether the execution-global context has fired (abort).
func (tc *tickContext) aborted() bool {
	return tc.rt.ctx.Err() != nil
}

// canceled reports whether this specific tick was interrupted.
func (tc *tickContext) interrupted() bool {
	return tc.ctx.Err() != nil
}

// check aborts evaluation if the tick was interrupted or execution aborted.
func (tc *tickContext) check() error {
	if tc.aborted() {
		return errAborted
	}
	if tc.interrupted() {
		return errInterrupted
	}
	return nil
}
