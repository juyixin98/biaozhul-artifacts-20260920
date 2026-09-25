package deadlineadm

import "time"

// wakeKind distinguishes the scheduler's internal timed events.
type wakeKind int

const (
	wakeQueuedDeadline wakeKind = iota // a queued job reached its deadline
	wakeRunningBound                   // a running job reached its kill bound
)

// wakeEvent is one pending scheduler-internal timed action.
type wakeEvent struct {
	at    time.Time
	kind  wakeKind
	jobID string
	seq   int64
	index int
}

// wakeHeap orders internal events (time, then seq for FIFO determinism).
type wakeHeap []*wakeEvent

func (h wakeHeap) Len() int { return len(h) }

func (h wakeHeap) Less(i, j int) bool {
	if !h[i].at.Equal(h[j].at) {
		return h[i].at.Before(h[j].at)
	}
	return h[i].seq < h[j].seq
}

func (h wakeHeap) Swap(i, j int) {
	h[i], h[j] = h[j], h[i]
	h[i].index = i
	h[j].index = j
}

func (h *wakeHeap) Push(x any) {
	e := x.(*wakeEvent)
	e.index = len(*h)
	*h = append(*h, e)
}

func (h *wakeHeap) Pop() any {
	old := *h
	n := len(old)
	e := old[n-1]
	old[n-1] = nil
	e.index = -1
	*h = old[:n-1]
	return e
}
