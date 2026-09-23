package sim

import "container/heap"

// event is one scheduled callback. Equal-time events run in scheduling
// order (seq), which keeps execution deterministic.
type event struct {
	t   int64
	seq uint64
	fn  func()
}

type eventHeap []event

func (h eventHeap) Len() int { return len(h) }
func (h eventHeap) Less(i, j int) bool {
	if h[i].t != h[j].t {
		return h[i].t < h[j].t
	}
	return h[i].seq < h[j].seq
}
func (h eventHeap) Swap(i, j int) { h[i], h[j] = h[j], h[i] }
func (h *eventHeap) Push(x any)   { *h = append(*h, x.(event)) }
func (h *eventHeap) Pop() any {
	old := *h
	n := len(old)
	e := old[n-1]
	*h = old[:n-1]
	return e
}

// schedule pushes an event at logical time t.
func (s *Sim) schedule(t int64, fn func()) {
	if t < s.now {
		t = s.now
	}
	s.seq++
	heap.Push(&s.events, event{t: t, seq: s.seq, fn: fn})
}

// after schedules an event d ms in the future.
func (s *Sim) after(d int64, fn func()) { s.schedule(s.now+d, fn) }
