// Package sim is a minimal deterministic discrete-event scheduler.
// Time is an integer virtual clock; no real wall-clock time or goroutines are
// involved, so identical requests (including the seed) always replay
// identically.
package sim

import (
	"container/heap"
)

// Event is one scheduled occurrence. Kind-specific payloads live in the
// packages that own the event; the scheduler treats Data as opaque.
type Event struct {
	Time  int64
	Kind  string
	Data  any
	seq   int64 // insertion counter, tie-breaks equal timestamps (FIFO)
	index int   // heap bookkeeping
}

type eventHeap []*Event

func (h eventHeap) Len() int { return len(h) }
func (h eventHeap) Less(i, j int) bool {
	if h[i].Time != h[j].Time {
		return h[i].Time < h[j].Time
	}
	return h[i].seq < h[j].seq
}
func (h eventHeap) Swap(i, j int) {
	h[i], h[j] = h[j], h[i]
	h[i].index = i
	h[j].index = j
}
func (h *eventHeap) Push(x any) {
	e := x.(*Event)
	e.index = len(*h)
	*h = append(*h, e)
}
func (h *eventHeap) Pop() any {
	old := *h
	n := len(old)
	e := old[n-1]
	old[n-1] = nil
	e.index = -1
	*h = old[:n-1]
	return e
}

// Scheduler processes events in (time, insertion order) order.
type Scheduler struct {
	h   eventHeap
	cnt int64
	now int64
}

// NewScheduler creates an empty scheduler with virtual time starting at 0.
func NewScheduler() *Scheduler {
	s := &Scheduler{}
	heap.Init(&s.h)
	return s
}

// At schedules an event at absolute time t. Events with the same time fire in
// insertion order.
func (s *Scheduler) At(t int64, kind string, data any) *Event {
	s.cnt++
	e := &Event{Time: t, Kind: kind, Data: data, seq: s.cnt}
	heap.Push(&s.h, e)
	return e
}

// After schedules an event relative to the current virtual time.
func (s *Scheduler) After(d int64, kind string, data any) *Event {
	return s.At(s.now+d, kind, data)
}

// Now returns the current virtual time.
func (s *Scheduler) Now() int64 { return s.now }

// Run pops events with Time <= endTime, invoking handle for each. Events
// scheduled at exactly endTime still fire; handlers that schedule follow-ups
// at the current time (e.g. chained broadcasts) keep firing because they are
// inserted before the pre-scheduled sentinel placed past endTime (FIFO at
// equal timestamps).
func (s *Scheduler) Run(endTime int64, handle func(*Event)) {
	for len(s.h) > 0 && s.h[0].Time <= endTime {
		e := heap.Pop(&s.h).(*Event)
		s.now = e.Time
		handle(e)
	}
}
