package batchagg

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"
)

// Sentinel errors returned at submission time. Callers can use errors.Is.
var (
	// ErrOversizeItem means a single item's payload is larger than MaxBytes
	// and can therefore never fit into any batch. Such items are refused
	// immediately instead of being split or retried.
	ErrOversizeItem = errors.New("item payload exceeds configured max batch bytes")
	// ErrEmptyKey means an item was submitted without a compatibility key.
	ErrEmptyKey = errors.New("compatibility key must not be empty")
	// ErrClosed means the scheduler has been shut down.
	ErrClosed = errors.New("scheduler is closed")
)

// Item is one unit of work submitted to the scheduler. Items with the same
// Key are guaranteed to be aggregated together; items with different keys are
// always aggregated separately.
type Item struct {
	// ID uniquely identifies the item. If empty, the scheduler assigns one.
	ID string
	// Key is the compatibility key used for batching.
	Key string
	// Payload is counted against MaxBytes verbatim (len(Payload)).
	Payload []byte
}

// ItemResult is one item's independent outcome. Even when several items ride
// in the same batch, each item receives its own ItemResult (or its own error).
type ItemResult struct {
	// Index is the item's position within the executed batch.
	Index int
	// BatchID identifies the batch that produced this result.
	BatchID string
	// Output is the item-specific output; nil for failed items.
	Output []byte
	// Err is this item's individual error. A nil error means success, even
	// when other items in the same batch failed.
	Err error
}

// Executor executes one flushed batch.
//
// Contract:
//   - Execute is called with between 1 and MaxItems items whose total payload
//     size is at most MaxBytes.
//   - On success it returns one *ItemResult per item, in the same order. A
//     result's own Err may be non-nil, which marks that single item as failed
//     without affecting the others (partial batch failure).
//   - Returning a non-nil error fails every item in the batch with that error.
//   - Implementations must be safe for concurrent calls across batches.
type Executor interface {
	Execute(ctx context.Context, batchID string, items []Item) ([]*ItemResult, error)
}

// Config configures a Scheduler. Zero values are replaced with defaults when
// New is called.
type Config struct {
	// MaxItems flushes a batch as soon as it holds this many items.
	MaxItems int
	// MaxBytes flushes a batch as soon as the accumulated payload reaches
	// this size. A single item larger than MaxBytes is rejected.
	MaxBytes int
	// MaxWait is the longest time a batch may wait for more items after its
	// first item arrived.
	MaxWait time.Duration
	// Clock is replaced with NewSystemClock() when nil; tests inject a
	// VirtualClock.
	Clock Clock
	// Sink receives structured state-change events; defaults to NopSink.
	Sink EventSink
}

// Scheduler aggregates submitted items per compatibility key and flushes each
// key's queue when an item-count, byte-count or wait-time threshold is hit.
type Scheduler struct {
	cfg     Config
	exec    Executor
	idSeq   uint64
	itemSeq uint64

	mu      sync.Mutex
	queues  map[string]chan msg
	closed  bool
	closeCh chan struct{}

	// wg tracks every per-key runner goroutine.
	wg sync.WaitGroup
}

// New creates a Scheduler with defaults applied.
func New(cfg Config, exec Executor) *Scheduler {
	if cfg.MaxItems <= 0 {
		cfg.MaxItems = 32
	}
	if cfg.MaxBytes <= 0 {
		cfg.MaxBytes = 4 * 1024 * 1024
	}
	if cfg.MaxWait <= 0 {
		cfg.MaxWait = 20 * time.Millisecond
	}
	if cfg.Clock == nil {
		cfg.Clock = NewSystemClock()
	}
	if cfg.Sink == nil {
		cfg.Sink = NopSink()
	}
	return &Scheduler{
		cfg:     cfg,
		exec:    exec,
		queues:  make(map[string]chan msg),
		closeCh: make(chan struct{}),
	}
}

// queueBuffer bounds how many submissions/cancels may be queued for a key while
// a batch is executing. The scheduler applies backpressure to Submit once a
// key's buffer is full.
const queueBuffer = 1024

// Submit enqueues an item for aggregation. It blocks only on scheduler
// backpressure or on ctx while waiting to enqueue; it never waits for the item
// to execute. The returned Future resolves with this item's independent result.
//
// Rejection (oversize item, empty key, closed scheduler) is returned as an
// error here and additionally recorded as an "rejected" event. Cancellation via
// ctx after successful enqueue removes just this item from its pending batch;
// it never affects other items, nor an already-dispatched batch.
func (s *Scheduler) Submit(ctx context.Context, item Item) (*Future, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	if item.Key == "" {
		s.emit(Event{Type: EventRejected, ItemID: item.ID, Reason: "empty_key"})
		return nil, ErrEmptyKey
	}
	if len(item.Payload) > s.cfg.MaxBytes {
		s.emit(Event{
			Type:     EventRejected,
			Key:      item.Key,
			ItemID:   item.ID,
			Reason:   "oversize",
			Bytes:    len(item.Payload),
			MaxBytes: s.cfg.MaxBytes,
		})
		return nil, fmt.Errorf("%w: item %q is %d bytes, limit is %d",
			ErrOversizeItem, item.ID, len(item.Payload), s.cfg.MaxBytes)
	}
	if item.ID == "" {
		item.ID = fmt.Sprintf("item-%d", atomic.AddUint64(&s.itemSeq, 1))
	}
	fut := newFuture(item)

	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		s.emit(Event{Type: EventRejected, Key: item.Key, ItemID: item.ID, Reason: "closed"})
		return nil, ErrClosed
	}
	q, ok := s.queues[item.Key]
	if !ok {
		q = make(chan msg, queueBuffer)
		s.queues[item.Key] = q
		s.wg.Add(1)
		go s.runKey(item.Key, q)
	}
	s.mu.Unlock()

	select {
	case q <- msg{sub: &submitMsg{item: item, fut: fut}}:
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-s.closeCh:
		return nil, ErrClosed
	}

	// Propagate caller-side cancellation to the pending queue. Dispatched
	// items ignore this: their outcome is already determined.
	if ctx.Done() != nil {
		go s.watchCancel(ctx, item.Key, item.ID, fut)
	}
	return fut, nil
}

// Close stops accepting items and flushes every pending batch once (reason
// "shutdown"), delivering each item's result. It blocks until all runners and
// in-flight batches have finished. Calling Close more than once is a no-op.
func (s *Scheduler) Close() error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	close(s.closeCh)
	s.mu.Unlock()
	s.wg.Wait()
	return nil
}

func (s *Scheduler) newBatchID() string {
	return fmt.Sprintf("batch-%d", atomic.AddUint64(&s.idSeq, 1))
}

func (s *Scheduler) emit(e Event) {
	if e.At.IsZero() {
		e.At = s.cfg.Clock.Now()
	}
	s.cfg.Sink.Emit(e)
}

// --- internal message types ------------------------------------------------

type submitMsg struct {
	item Item
	fut  *Future
}

type cancelMsg struct {
	id    string
	cause error
}

type msg struct {
	sub    *submitMsg
	cancel *cancelMsg
}

// --- per-key runner --------------------------------------------------------

type pendingBatch struct {
	id       string
	key      string
	items    []Item
	futs     []*Future
	bytes    int
	openedAt time.Time
}

func (s *Scheduler) runKey(key string, q chan msg) {
	defer s.wg.Done()

	var b *pendingBatch
	var timer Timer
	var timerC <-chan time.Time

	closeBatch := func(reason string) {
		timer.Stop()
		timer, timerC = nil, nil
		s.emit(Event{
			Type:     EventBatchFlush,
			At:       s.cfg.Clock.Now(),
			Key:      key,
			BatchID:  b.id,
			Reason:   reason,
			Items:    len(b.items),
			Bytes:    b.bytes,
			MaxItems: s.cfg.MaxItems,
			MaxBytes: s.cfg.MaxBytes,
		})
		batch := b
		b = nil
		// Execute inline: a key's batches run in arrival order. Other keys
		// have their own runners and proceed independently.
		s.executeBatch(batch)
	}

	// onMsg processes one submission or cancellation. It is used both for
	// live messages and for the queue drain during shutdown.
	onMsg := func(m msg) {
		switch {
		case m.sub != nil:
			sm := m.sub
			if b == nil {
				b = &pendingBatch{
					id:       s.newBatchID(),
					key:      key,
					openedAt: s.cfg.Clock.Now(),
				}
				timer = s.cfg.Clock.NewTimer(s.cfg.MaxWait)
				timerC = timer.C()
				s.emit(Event{
					Type:     EventBatchOpen,
					Key:      key,
					BatchID:  b.id,
					MaxItems: s.cfg.MaxItems,
					MaxBytes: s.cfg.MaxBytes,
				})
			}
			b.items = append(b.items, sm.item)
			b.futs = append(b.futs, sm.fut)
			b.bytes += len(sm.item.Payload)
			s.emit(Event{
				Type:    EventSubmitted,
				Key:     key,
				ItemID:  sm.item.ID,
				BatchID: b.id,
				Items:   len(b.items),
				Bytes:   b.bytes,
			})
			switch {
			case len(b.items) >= s.cfg.MaxItems:
				closeBatch("items")
			case b.bytes >= s.cfg.MaxBytes:
				closeBatch("bytes")
			}
		case m.cancel != nil:
			if b == nil {
				return
			}
			idx := -1
			for i, it := range b.items {
				if it.ID == m.cancel.id {
					idx = i
					break
				}
			}
			if idx < 0 {
				// Already dispatched: cancellation cannot change its
				// result and must not touch any other item.
				return
			}
			b.futs[idx].settle(nil, m.cancel.cause)
			b.bytes -= len(b.items[idx].Payload)
			b.items = append(b.items[:idx], b.items[idx+1:]...)
			b.futs = append(b.futs[:idx], b.futs[idx+1:]...)
			s.emit(Event{
				Type:    EventItemCanceled,
				Key:     key,
				ItemID:  m.cancel.id,
				BatchID: b.id,
				Items:   len(b.items),
				Bytes:   b.bytes,
				Err:     m.cancel.cause.Error(),
			})
			if len(b.items) == 0 {
				timer.Stop()
				timer, timerC = nil, nil
				s.emit(Event{
					Type:    EventBatchFlush,
					Key:     key,
					BatchID: b.id,
					Reason:  "cancel",
					Items:   0,
					Bytes:   0,
				})
				b = nil
			}
		}
	}

	for {
		select {
		case m := <-q:
			onMsg(m)
		case now := <-timerC:
			if b != nil {
				s.emit(Event{
					Type:     EventBatchFlush,
					At:       now,
					Key:      key,
					BatchID:  b.id,
					Reason:   "wait",
					Items:    len(b.items),
					Bytes:    b.bytes,
					MaxItems: s.cfg.MaxItems,
					MaxBytes: s.cfg.MaxBytes,
				})
				timer, timerC = nil, nil
				batch := b
				b = nil
				s.executeBatchAt(batch, now)
			}
		case <-s.closeCh:
			// Flush every submission already accepted into this key's queue.
			// Once closeCh is closed, Submit/Cancel senders abort on it too,
			// so the buffer cannot gain new entries while we drain.
			for len(q) > 0 {
				onMsg(<-q)
			}
			if b != nil {
				closeBatch("shutdown")
			}
			return
		}
	}
}

// --- batch execution --------------------------------------------------------

func (s *Scheduler) executeBatch(b *pendingBatch) {
	s.executeBatchAt(b, s.cfg.Clock.Now())
}

func (s *Scheduler) executeBatchAt(b *pendingBatch, at time.Time) {
	results, execErr := s.exec.Execute(context.Background(), b.id, b.items)

	// Whole-batch failure, protocol violation, or result-count mismatch:
	// every item gets the same error, independently of its siblings.
	if execErr != nil || results == nil || len(results) != len(b.items) {
		err := execErr
		if err == nil {
			err = fmt.Errorf("executor returned %d results for %d items (batch %s)",
				len(results), len(b.items), b.id)
		}
		for i, fut := range b.futs {
			r := &ItemResult{Index: i, BatchID: b.id, Err: err}
			fut.settle(r, err)
			s.emit(Event{
				Type:    EventItemResult,
				At:      at,
				Key:     b.key,
				ItemID:  b.items[i].ID,
				BatchID: b.id,
				Success: false,
				Err:     err.Error(),
			})
		}
		return
	}

	// Per-item outcomes: one item's failure never fails its siblings.
	for i, fut := range b.futs {
		r := results[i]
		if r == nil {
			r = &ItemResult{Index: i, BatchID: b.id}
		}
		r.Index = i
		r.BatchID = b.id
		fut.settle(r, r.Err)
		s.emit(Event{
			Type:    EventItemResult,
			At:      at,
			Key:     b.key,
			ItemID:  b.items[i].ID,
			BatchID: b.id,
			Success: r.Err == nil,
			Err:     errString(r.Err),
		})
	}
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

// watchCancel removes a not-yet-dispatched item when its submitter goes away.
func (s *Scheduler) watchCancel(ctx context.Context, key, id string, fut *Future) {
	ch := fut.done()
	select {
	case <-ctx.Done():
	case <-ch:
		return
	case <-s.closeCh:
		return
	}
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	q := s.queues[key]
	s.mu.Unlock()
	select {
	case q <- msg{cancel: &cancelMsg{id: id, cause: ctx.Err()}}:
	case <-s.closeCh:
	}
}

// --- Future ----------------------------------------------------------------

// Future is an item's pending result.
type Future struct {
	item Item

	mu      sync.Mutex
	settled bool
	out     outcome
	ch      chan struct{}
}

type outcome struct {
	res     *ItemResult
	waitErr error // used only when res == nil (pre-dispatch cancellation)
}

func newFuture(item Item) *Future {
	return &Future{item: item, ch: make(chan struct{}, 1)}
}

// Item returns the item this future belongs to.
func (f *Future) Item() Item { return f.item }

// Get blocks until the item's independent result is available, ctx ends, or
// (for cancellation after dispatch) the caller gives up. Cancelling ctx while
// waiting for an executed result does not cancel the item itself.
func (f *Future) Get(ctx context.Context) (*ItemResult, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	f.mu.Lock()
	if f.settled {
		res := f.out.res
		waitErr := f.out.waitErr
		f.mu.Unlock()
		if res == nil {
			return nil, waitErr
		}
		return res, nil
	}
	f.mu.Unlock()

	select {
	case <-f.ch:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	f.mu.Lock()
	res := f.out.res
	waitErr := f.out.waitErr
	f.mu.Unlock()
	// An executed item always carries its independent ItemResult, including
	// per-item execution failures (res.Err). The returned Go error is reserved
	// for non-result conditions: pre-dispatch cancellation (res == nil).
	if res == nil {
		return nil, waitErr
	}
	return res, nil
}

func (f *Future) settle(res *ItemResult, err error) {
	f.mu.Lock()
	if f.settled {
		f.mu.Unlock()
		return
	}
	f.settled = true
	if res == nil {
		// Pre-dispatch cancellation: no result exists; surface the cause on Get.
		f.out = outcome{waitErr: err}
	} else {
		f.out = outcome{res: res}
	}
	f.mu.Unlock()
	select {
	case f.ch <- struct{}{}:
	default:
	}
}

// done returns a channel closed when the future has settled.
func (f *Future) done() <-chan struct{} { return f.ch }
