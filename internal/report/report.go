// Package report collects a structured, per-request event timeline from every
// layer in the in-process mesh. It is the machine-readable evidence used by
// the acceptance fixtures and the demo output.
package report

import (
	"sync"
	"time"
)

// Event types.
const (
	EvAttemptStart = "attempt-start"
	EvAttemptEnd   = "attempt-end"
	EvBackoff      = "backoff"
	EvOutcome      = "outcome"
)

// Event is one thing that happened while serving a request.
type Event struct {
	Seq       int           `json:"seq"`
	Time      time.Time     `json:"time"`
	Elapsed   time.Duration `json:"-"`
	ElapsedMS int64         `json:"elapsed_ms"`
	Layer     string        `json:"layer"`
	Type      string        `json:"type"`
	Attempt   int           `json:"attempt,omitempty"`
	Detail    string        `json:"detail,omitempty"`
}

// Collector stores timelines keyed by request ID.
type Collector struct {
	mu     sync.Mutex
	seq    int
	events map[string][]Event
	start  map[string]time.Time
}

func NewCollector() *Collector {
	return &Collector{events: map[string][]Event{}, start: map[string]time.Time{}}
}

// Add records an event for requestID at time t.
func (c *Collector) Add(t time.Time, requestID, layer, typ string, attempt int, detail string) Event {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.start[requestID]; !ok {
		c.start[requestID] = t
	}
	c.seq++
	elapsed := t.Sub(c.start[requestID])
	e := Event{
		Seq:       c.seq,
		Time:      t,
		Elapsed:   elapsed,
		ElapsedMS: elapsed.Milliseconds(),
		Layer:     layer,
		Type:      typ,
		Attempt:   attempt,
		Detail:    detail,
	}
	c.events[requestID] = append(c.events[requestID], e)
	return e
}

// Timeline returns a copy of the events recorded for requestID.
func (c *Collector) Timeline(requestID string) []Event {
	c.mu.Lock()
	defer c.mu.Unlock()
	src := c.events[requestID]
	out := make([]Event, len(src))
	copy(out, src)
	return out
}
