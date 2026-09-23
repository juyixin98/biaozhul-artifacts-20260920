package sim

import "container/heap"

// eventHeap is a min-heap ordered by (time, insertion sequence).
type eventHeap []*event

func (h eventHeap) Len() int { return len(h) }

func (h eventHeap) Less(i, j int) bool {
	if h[i].time != h[j].time {
		return h[i].time < h[j].time
	}
	return h[i].seq < h[j].seq
}

func (h eventHeap) Swap(i, j int) { h[i], h[j] = h[j], h[i] }

func (h *eventHeap) Push(x any) { *h = append(*h, x.(*event)) }

func (h *eventHeap) Pop() any {
	old := *h
	n := len(old)
	x := old[n-1]
	old[n-1] = nil
	*h = old[:n-1]
	return x
}

var _ heap.Interface = (*eventHeap)(nil)

// push is a small helper to keep the run loop readable.
func (s *Sim) push(e *event) {
	e.seq = s.counter
	s.counter++
	heap.Push(&s.events, e)
}
