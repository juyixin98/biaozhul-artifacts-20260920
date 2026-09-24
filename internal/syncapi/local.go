package syncapi

import (
	"context"

	"merklesync/internal/store"
)

// Local is the synchronizer's view of the local replica. The demo uses
// DirectLocal (an in-process *store.Store); the CLI's `sync` command drives
// two already-running servers through HTTPLocal.
type Local interface {
	Snapshot() store.Snapshot
	// ApplyEntries merges records under LWW and returns how many won.
	ApplyEntries(entries []store.Entry) int
}

// DirectLocal wraps an in-process store.
type DirectLocal struct{ St *store.Store }

func (d DirectLocal) Snapshot() store.Snapshot { return d.St.Snapshot() }

func (d DirectLocal) ApplyEntries(entries []store.Entry) int {
	return len(d.St.ApplyEntries(entries))
}

// HTTPLocal presents a remote server (one we own) as a Local. Snapshot()
// serves the last fetched state; Refresh pulls a fresh one, and ApplyEntries
// pushes the batch then refreshes so the next round sees up-to-date state.
type HTTPLocal struct {
	c    *Client
	ctx  context.Context
	snap store.Snapshot
}

func NewHTTPLocal(ctx context.Context, c *Client) *HTTPLocal {
	return &HTTPLocal{c: c, ctx: ctx}
}

func (h *HTTPLocal) Refresh() error {
	snap, err := h.c.Snapshot(h.ctx)
	if err != nil {
		return err
	}
	h.snap = snap
	return nil
}

func (h *HTTPLocal) Snapshot() store.Snapshot { return h.snap }

func (h *HTTPLocal) ApplyEntries(entries []store.Entry) int {
	n, err := h.c.Apply(h.ctx, entries)
	if err != nil {
		return 0
	}
	if err := h.Refresh(); err != nil {
		// Best effort: a stale view only costs an extra round.
		_ = err
	}
	return n
}
