// Package fakesvc provides the in-process fake downstream dependency
// (an audit-log service) with fault injection, so the project never
// talks to any production system.
package fakesvc

import (
	"errors"
	"sync"
	"time"

	"conditionupdate/internal/clock"
)

// ErrInjected is returned when a call is failed by fault injection.
var ErrInjected = errors.New("injected downstream failure")

// AuditEntry is one recorded change.
type AuditEntry struct {
	ResourceID string    `json:"resourceId"`
	Version    uint64    `json:"version"`
	ETag       string    `json:"etag"`
	At         time.Time `json:"at"`
}

// Faults describes the active fault-injection settings.
type Faults struct {
	// FailNext fails the next N Record calls with ErrInjected.
	FailNext int `json:"failNext"`
	// LatencyMs adds an artificial delay to every Record call.
	LatencyMs int `json:"latencyMs"`
}

// AuditService is the fake downstream audit log.
type AuditService struct {
	mu      sync.Mutex
	clock   clock.Clock
	faults  Faults
	entries []AuditEntry
}

// NewAudit returns an empty fake audit service.
func NewAudit(c clock.Clock) *AuditService {
	return &AuditService{clock: c}
}

// Record appends an entry, unless fault injection makes it fail.
func (a *AuditService) Record(e AuditEntry) error {
	a.mu.Lock()
	if a.faults.LatencyMs > 0 {
		time.Sleep(time.Duration(a.faults.LatencyMs) * time.Millisecond)
	}
	if a.faults.FailNext > 0 {
		a.faults.FailNext--
		a.mu.Unlock()
		return ErrInjected
	}
	a.entries = append(a.entries, e)
	a.mu.Unlock()
	return nil
}

// SetFaults replaces the fault-injection settings.
func (a *AuditService) SetFaults(f Faults) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.faults = f
}

// GetFaults returns the current fault-injection settings.
func (a *AuditService) GetFaults() Faults {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.faults
}

// Entries returns a copy of all recorded entries.
func (a *AuditService) Entries() []AuditEntry {
	a.mu.Lock()
	defer a.mu.Unlock()
	return append([]AuditEntry(nil), a.entries...)
}
