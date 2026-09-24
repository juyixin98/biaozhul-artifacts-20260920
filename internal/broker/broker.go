// Package broker fans published events out to live SSE subscribers on top of
// the durable event store.
//
// No-gap, no-duplicate guarantee at the replay/live seam:
//
//  1. Subscribe holds the broker lock while it (a) reads the history snapshot
//     and (b) registers the subscriber. Publish appends to the store AND fans
//     out while holding the same lock. Hence an event is either in the
//     snapshot or pushed live, never both.
//  2. The HTTP handler writes the snapshot to the network WITHOUT holding the
//     lock. While a subscriber is in the "replaying" phase, live events are
//     appended to its bounded pending list instead of the live channel.
//  3. Activate atomically flips the subscriber to live: pending events are
//     handed to the handler first, then channel events — strict ID order.
//
// Slow-consumer policy is non-blocking everywhere: a full pending list during
// replay or a full live channel during streaming force-disconnects that one
// subscriber; the publisher is never stalled.
package broker

import (
	"context"
	"fmt"
	"log"
	"sync"
	"time"

	"sseserver/internal/store"
)

// Reset instructs the client that its Last-Event-ID is no longer usable.
type Reset struct {
	Reason string // "expired" | "future"
	Oldest uint64 // oldest ID currently retained
	Last   uint64 // newest ID currently retained
}

// Subscriber is one live SSE connection.
type Subscriber struct {
	id     uint64
	items  chan store.Event // live events (after activation)
	stop   chan struct{}    // closed on force-disconnect
	dropMu sync.Mutex
	drop   string // "" when alive, otherwise reason

	// The following fields are guarded by Broker.mu.
	active  bool
	pending []store.Event // live events that arrived while replay was draining
}

func newSubscriber(id uint64, buffer int) *Subscriber {
	return &Subscriber{
		id:    id,
		items: make(chan store.Event, buffer),
		stop:  make(chan struct{}),
	}
}

// Items returns the live-event channel (valid after activation).
func (s *Subscriber) Items() <-chan store.Event { return s.items }

// ID returns the broker-assigned subscriber number (for logs).
func (s *Subscriber) ID() uint64 { return s.id }

// Done is closed when the subscriber has been force-removed. The HTTP handler
// should then read DropReason and close the connection.
func (s *Subscriber) Done() <-chan struct{} { return s.stop }

// DropReason returns "" for a live subscriber, otherwise why it was dropped
// ("slow_consumer").
func (s *Subscriber) DropReason() string {
	s.dropMu.Lock()
	defer s.dropMu.Unlock()
	return s.drop
}

func (s *Subscriber) forceDrop(reason string) bool {
	s.dropMu.Lock()
	defer s.dropMu.Unlock()
	if s.drop != "" {
		return false
	}
	s.drop = reason
	close(s.stop)
	return true
}

// Config holds broker limits.
type Config struct {
	// BufferSize bounds both the live channel capacity and the replay-phase
	// pending list. A subscriber that falls this many events behind is
	// force-disconnected.
	BufferSize int
}

// Subscription is the result of Subscribe.
type Subscription struct {
	Sub      *Subscriber
	Replay   []store.Event // events with ID > LastEventID, write these first
	LiveOnly bool          // headerless subscription: already active, no replay
	Reset    *Reset        // when set, the cursor is unusable; no Sub is registered
}

// Broker serialises publishes and subscriptions over one event log.
type Broker struct {
	st  *store.Store
	cfg Config

	mu       sync.Mutex
	nextSub  uint64
	subs     map[uint64]*Subscriber
	slowDrop uint64
	logger   *log.Logger
}

// New creates a broker over st.
func New(st *store.Store, cfg Config, logger *log.Logger) *Broker {
	if cfg.BufferSize <= 0 {
		cfg.BufferSize = 64
	}
	if logger == nil {
		logger = log.Default()
	}
	return &Broker{st: st, cfg: cfg, subs: make(map[uint64]*Subscriber), logger: logger}
}

// Subscribe creates a subscription.
//
// replay=false means "no Last-Event-ID was supplied": the client is attached
// for live events only and receives no history.
//
// When replay=true, lastID is the resume cursor:
//   - lastID == oldest-1: valid cursor asking for the whole retained log;
//   - lastID+1 < oldest: expired/gap — result.Reset explains the reset;
//   - lastID > current last event: reset (future cursor);
//   - otherwise: retained events with ID > lastID are returned as Replay,
//     followed seamlessly by live events after Activate.
func (b *Broker) Subscribe(lastID uint64, replay bool) Subscription {
	b.mu.Lock()
	defer b.mu.Unlock()

	if replay {
		bounds := b.st.Bounds()
		if !bounds.Empty {
			switch {
			case lastID+1 < bounds.Oldest:
				return Subscription{Reset: &Reset{Reason: "expired", Oldest: bounds.Oldest, Last: bounds.Last}}
			case lastID > bounds.Last:
				return Subscription{Reset: &Reset{Reason: "future", Oldest: bounds.Oldest, Last: bounds.Last}}
			}
		}
	}

	b.nextSub++
	sub := newSubscriber(b.nextSub, b.cfg.BufferSize)
	// Preallocate pending at exactly the buffer capacity. Starting from nil
	// would let append double the backing array (nil -> cap 1 -> 2 -> 4...),
	// allowing len to overshoot BufferSize before the overflow check fires.
	sub.pending = make([]store.Event, 0, b.cfg.BufferSize)
	// A live-only subscription (no cursor) has no history to drain, so make
	// it active immediately: events go straight to the bounded channel and
	// the slow-consumer limit is just the channel capacity, with no replay
	// window to widen it.
	if !replay {
		sub.active = true
	}
	// For replay subscriptions, Replay is the snapshot the handler drains
	// first; pending only collects events published during that drain. Since
	// returns a fresh slice so the handler's lock-free reads never share an
	// array with the broker's locked pending appends.
	var replayEvents []store.Event
	if replay {
		replayEvents = b.st.Since(lastID, 0)
	}
	b.subs[sub.id] = sub
	return Subscription{Sub: sub, Replay: replayEvents, LiveOnly: !replay}
}

// Activate marks replay draining complete. It returns the pending events that
// arrived during replay (in ID order) and false if the subscriber was
// force-dropped while replaying. After a successful activation live events
// arrive on Items().
func (b *Broker) Activate(sub *Subscriber) ([]store.Event, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if sub.DropReason() != "" {
		return nil, false
	}
	pending := sub.pending
	sub.pending = nil
	sub.active = true
	return pending, true
}

// Unsubscribe removes a subscriber on normal client disconnect. Safe to call
// multiple times and for a subscriber that was force-dropped.
func (b *Broker) Unsubscribe(sub *Subscriber) {
	b.mu.Lock()
	delete(b.subs, sub.id)
	b.mu.Unlock()
}

// Publish appends data to the log (assigning the next monotonic ID) and fans
// it out. The store append happens under the broker lock so that it cannot
// interleave with Subscribe's history read; disk I/O therefore briefly
// serialises against new subscriptions, which is acceptable for this
// single-stream service. Slow subscribers are dropped, never stalled on.
func (b *Broker) Publish(ctx context.Context, data string) (store.Event, error) {
	b.mu.Lock()
	ev, err := b.st.Append(data, time.Now().UTC())
	if err != nil {
		b.mu.Unlock()
		return store.Event{}, fmt.Errorf("broker: append: %w", err)
	}

	var dropped []*Subscriber
	for id, sub := range b.subs {
		if !sub.active {
			// Replay phase: buffer alongside the snapshot, drop on overflow.
			if len(sub.pending) >= b.cfg.BufferSize {
				dropped = append(dropped, sub)
				delete(b.subs, id)
				continue
			}
			sub.pending = append(sub.pending, ev)
			continue
		}
		select {
		case sub.items <- ev:
		default:
			dropped = append(dropped, sub)
			delete(b.subs, id)
		}
	}
	b.mu.Unlock()

	for _, sub := range dropped {
		if sub.forceDrop("slow_consumer") {
			b.slowDrop++
			b.logger.Printf("broker: slow consumer subscriber=%d dropped at event=%d (buffer=%d full)",
				sub.id, ev.ID, b.cfg.BufferSize)
		}
	}
	return ev, nil
}

// SubscriberCount reports the current live subscriber count.
func (b *Broker) SubscriberCount() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.subs)
}

// SlowDrops reports how many subscribers were force-disconnected.
func (b *Broker) SlowDrops() uint64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.slowDrop
}

// DropAllForTest force-drops every current subscriber exactly as a buffer
// overflow would, returning the number newly dropped. It is intended for
// tests of the HTTP drop contract where reproducing a network stall over
// loopback is unreliable.
func (b *Broker) DropAllForTest() int {
	b.mu.Lock()
	var subs []*Subscriber
	for id, sub := range b.subs {
		subs = append(subs, sub)
		delete(b.subs, id)
	}
	b.mu.Unlock()

	n := 0
	for _, sub := range subs {
		if sub.forceDrop("slow_consumer") {
			b.mu.Lock()
			b.slowDrop++
			b.mu.Unlock()
			n++
		}
	}
	return n
}
