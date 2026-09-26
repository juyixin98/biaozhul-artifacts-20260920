// Package notifier is an in-process fake for the external audit dependency.
//
// AuditLog plays the role of the production downstream service (it is just an
// in-memory append log here; nothing leaves the process). FaultyNotifier wraps
// it and deterministically injects failures and latency so the server's retry
// behavior and the "no side effects on failure" guarantees can be exercised
// without any real network.
package notifier

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"time"

	"etagrace/internal/clock"
)

// ErrInjected is the synthetic failure returned while faults are armed.
var ErrInjected = errors.New("injected downstream failure")

// Event is one audited state change.
type Event struct {
	Type       string    `json:"type"`
	Key        string    `json:"key"`
	Version    int64     `json:"version"`
	ETag       string    `json:"etag"`
	OccurredAt time.Time `json:"occurredAt"`
}

// AuditLog is the fake downstream service. It records events durably enough
// for the demo: events are visible under mutex and can be snapshotted.
type AuditLog struct {
	mu     sync.Mutex
	events []Event
}

func NewAuditLog() *AuditLog { return &AuditLog{} }

func (l *AuditLog) appendLocked(e Event) {
	l.events = append(l.events, e)
}

func (l *AuditLog) Append(e Event) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.appendLocked(e)
}

// Events returns a copy of all recorded events.
func (l *AuditLog) Events() []Event {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]Event(nil), l.events...)
}

// Len returns how many events have been recorded.
func (l *AuditLog) Len() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.events)
}

// Notifier is the interface the server depends on.
type Notifier interface {
	Notify(ctx context.Context, e Event) error
}

// FaultyNotifier wraps AuditLog and injects failures deterministically.
// Arm failures with ArmFailures: n > 0 fails exactly the next n calls;
// n < 0 fails every call until disarmed. Latency is advanced on the
// injected clock so tests stay instant.
type FaultyNotifier struct {
	log      *AuditLog
	clk      clock.Clock
	failures atomic.Int32
	Latency  time.Duration
}

func NewFaulty(log *AuditLog, clk clock.Clock) *FaultyNotifier {
	return &FaultyNotifier{log: log, clk: clk}
}

// ArmFailures makes the next n Notify calls fail. Pass a negative number to
// fail every call; call Disarm to recover.
func (f *FaultyNotifier) ArmFailures(n int) {
	f.failures.Store(int32(n))
}

// ReliableNotifier never fails. The standalone server uses it.
type ReliableNotifier struct{ log *AuditLog }

func NewReliable(log *AuditLog) *ReliableNotifier { return &ReliableNotifier{log: log} }

func (r *ReliableNotifier) Notify(_ context.Context, e Event) error {
	r.log.Append(e)
	return nil
}
