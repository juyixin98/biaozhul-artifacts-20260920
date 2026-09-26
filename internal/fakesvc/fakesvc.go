// Package fakesvc provides in-process fake external services (a fake DB and
// a fake queue). They simulate latency through the shared clock, honour
// context cancellation, and record their own close so tests can verify
// resource shutdown order. No production systems are contacted.
package fakesvc

import (
	"context"
	"fmt"
	"sync/atomic"
	"time"

	"graceful-shutdown/internal/clock"
)

// FakeDB simulates a latency-bound query service.
type FakeDB struct {
	clk        clock.Clock
	closedFlag atomic.Bool
	queries    atomic.Uint64
}

func NewFakeDB(clk clock.Clock) *FakeDB { return &FakeDB{clk: clk} }

func (d *FakeDB) Name() string { return "fake-db" }

// Query sleeps for the simulated latency, returning early if ctx is
// cancelled (e.g. by the shutdown coordinator's cancel phase).
func (d *FakeDB) Query(ctx context.Context, query string) (string, error) {
	if d.closedFlag.Load() {
		return "", fmt.Errorf("fake-db is closed")
	}
	d.queries.Add(1)
	select {
	case <-d.clk.After(50 * time.Millisecond):
		return fmt.Sprintf("result(%s)", query), nil
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

// SlowQuery simulates a long-running query with configurable latency.
func (d *FakeDB) SlowQuery(ctx context.Context, query string, latency time.Duration) (string, error) {
	if d.closedFlag.Load() {
		return "", fmt.Errorf("fake-db is closed")
	}
	d.queries.Add(1)
	select {
	case <-d.clk.After(latency):
		return fmt.Sprintf("result(%s)", query), nil
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

func (d *FakeDB) Close(ctx context.Context) error {
	d.closedFlag.Store(true)
	return nil
}

// FakeQueue simulates a background-task queue. Publish fails once closed.
type FakeQueue struct {
	clk        clock.Clock
	closedFlag atomic.Bool
	published  atomic.Uint64
}

func NewFakeQueue(clk clock.Clock) *FakeQueue { return &FakeQueue{clk: clk} }

func (q *FakeQueue) Name() string { return "fake-queue" }

func (q *FakeQueue) Publish(ctx context.Context, msg string) error {
	if q.closedFlag.Load() {
		return fmt.Errorf("fake-queue is closed")
	}
	q.published.Add(1)
	select {
	case <-q.clk.After(10 * time.Millisecond):
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (q *FakeQueue) Close(ctx context.Context) error {
	q.closedFlag.Store(true)
	return nil
}
