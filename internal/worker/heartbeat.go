package worker

import (
	"context"
	"errors"
	"sync"
	"time"

	"sitevitals/internal/store"
)

// heartbeat periodically extends one lease and exposes a channel that closes
// when the lease has been lost (reaped/re-leased to another worker).
type heartbeat struct {
	q        *store.Queue
	jobID    uint64
	fence    int64
	holder   string
	interval time.Duration
	ttl      time.Duration

	mu      sync.Mutex
	stopped bool
	lostCh  chan struct{}
	once    sync.Once
	stopCh  chan struct{}
	doneCh  chan struct{}
}

func newHeartbeat(ctx context.Context, q *store.Queue, jobID uint64, fence int64,
	holder string, interval, ttl time.Duration) *heartbeat {
	return &heartbeat{
		q: q, jobID: jobID, fence: fence, holder: holder,
		interval: interval, ttl: ttl,
		stopCh: make(chan struct{}), doneCh: make(chan struct{}),
		lostCh: make(chan struct{}),
	}
}

func (h *heartbeat) start() {
	go func() {
		defer close(h.doneCh)
		t := time.NewTicker(h.interval)
		defer t.Stop()
		for {
			select {
			case <-h.stopCh:
				return
			case <-t.C:
				err := h.q.Heartbeat(context.Background(), h.jobID, h.fence, h.holder, h.ttl)
				if err != nil {
					if errors.Is(err, store.ErrLeaseLost) {
						h.markLost()
						return
					}
					// Transient DB error: retry on next tick; if the lease expires,
					// the reaper fences us out and the next call returns ErrLeaseLost.
				}
			}
		}
	}()
}

func (h *heartbeat) stop() {
	h.mu.Lock()
	if h.stopped {
		h.mu.Unlock()
		return
	}
	h.stopped = true
	h.mu.Unlock()
	close(h.stopCh)
	<-h.doneCh
}

func (h *heartbeat) lost() <-chan struct{} { return h.lostCh }

func (h *heartbeat) markLost() { h.once.Do(func() { close(h.lostCh) }) }
