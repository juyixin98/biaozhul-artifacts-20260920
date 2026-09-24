package syncapi

import (
	"context"
	"errors"
	"fmt"
	"net/http/httptest"
	"testing"

	"merklesync/internal/merkle"
	"merklesync/internal/store"
)

type testPair struct {
	t          *testing.T
	ctx        context.Context
	local      *store.Store
	peer       *store.Store
	localSrv   *httptest.Server
	peerSrv    *httptest.Server
	localURL   string
	peerURL    string
	peerClient *Client
}

func newTestPair(t *testing.T) *testPair {
	t.Helper()
	local, peer := store.New(), store.New()
	ls := httptest.NewServer(NewServer(local).Handler())
	ps := httptest.NewServer(NewServer(peer).Handler())
	t.Cleanup(ls.Close)
	t.Cleanup(ps.Close)
	return &testPair{
		t: t, ctx: context.Background(), local: local, peer: peer,
		localSrv: ls, peerSrv: ps, localURL: ls.URL, peerURL: ps.URL,
		peerClient: NewClient(ps.URL),
	}
}

func (p *testPair) syncer() *Syncer {
	return NewSyncer(DirectLocal{St: p.local}, NewClient(p.peerURL))
}

func seed(ctx context.Context, c *Client, n int) {
	for i := 0; i < n; i++ {
		if _, err := c.Put(ctx, fmt.Sprintf("key-%04d", i), "value", 1); err != nil {
			panic(err)
		}
	}
}

func rootsEqual(a, b *store.Store) bool {
	return merkle.Build(a.Snapshot()).Root() == merkle.Build(b.Snapshot()).Root()
}

func TestSyncNoOpWhenEqual(t *testing.T) {
	p := newTestPair(t)
	seed(p.ctx, NewClient(p.localURL), 50)
	seed(p.ctx, NewClient(p.peerURL), 50)

	c := NewClient(p.peerURL)
	sy := NewSyncer(DirectLocal{St: p.local}, c)
	res, err := sy.Sync(p.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Converged || res.Attempts != 1 {
		t.Fatalf("unexpected stats: %+v", res)
	}
	if c.Stats().NodeRequests != 0 || c.Stats().ValuesRecv != 0 {
		t.Fatalf("equal roots must not trigger tree traffic: %+v", c.Stats())
	}
}

func TestSyncSingleKeySparse(t *testing.T) {
	p := newTestPair(t)
	seed(p.ctx, NewClient(p.localURL), 200)
	seed(p.ctx, NewClient(p.peerURL), 200)
	pc := NewClient(p.peerURL)
	if _, err := pc.Put(p.ctx, "key-0066", "changed", 2); err != nil {
		t.Fatal(err)
	}

	sy := NewSyncer(DirectLocal{St: p.local}, NewClient(p.peerURL))
	res, err := sy.Sync(p.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Converged || res.DifferingBuckets != 1 || res.ValuesPulled != 1 || res.AppliedLocally != 1 {
		t.Fatalf("stats: %+v", res)
	}
	if e, _ := p.local.Get("key-0066"); e.Value != "changed" || e.Version != 2 {
		t.Fatalf("value not synced: %+v", e)
	}
	if !rootsEqual(p.local, p.peer) {
		t.Fatal("roots not equal after sync")
	}
	// One root exchange on an already-converged pair: nothing else moves.
	again, err := NewSyncer(DirectLocal{St: p.local}, NewClient(p.peerURL)).Sync(p.ctx)
	if err != nil || !again.Converged || again.Attempts != 1 || again.NodeHashesCompared != 0 {
		t.Fatalf("resync stats: %+v err=%v", again, err)
	}
}

func TestSyncBidirectionalUpdatesAndTombstones(t *testing.T) {
	p := newTestPair(t)
	seed(p.ctx, NewClient(p.localURL), 100)
	seed(p.ctx, NewClient(p.peerURL), 100)
	lc, pc := NewClient(p.localURL), NewClient(p.peerURL)

	// Peer: newer value on one key, tombstone on another.
	_, _ = pc.Put(p.ctx, "key-0001", "peer-v2", 2)
	_, _ = pc.DeleteKey(p.ctx, "key-0002", 2)
	// Local: newer value and a brand-new key (including a tombstone for a
	// key the peer holds live).
	_, _ = lc.Put(p.ctx, "key-0003", "local-v2", 2)
	_, _ = lc.Put(p.ctx, "local-only", "hi", 1)
	_, _ = lc.DeleteKey(p.ctx, "key-0004", 2)

	res, err := p.syncer().Sync(p.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Converged {
		t.Fatalf("did not converge: %+v", res)
	}
	if res.TombstonesExchanged < 2 {
		t.Fatalf("expected 2 tombstones exchanged, got %d", res.TombstonesExchanged)
	}
	if e, _ := p.local.Get("key-0001"); e.Value != "peer-v2" {
		t.Fatalf("peer value missing locally: %+v", e)
	}
	if e, _ := p.local.Get("key-0002"); !e.Deleted || e.Version != 2 {
		t.Fatalf("peer tombstone missing locally: %+v", e)
	}
	if e, _ := p.peer.Get("key-0003"); e.Value != "local-v2" {
		t.Fatalf("local value missing on peer: %+v", e)
	}
	if e, _ := p.peer.Get("local-only"); e.Value != "hi" {
		t.Fatalf("local-only key missing on peer: %+v", e)
	}
	if e, _ := p.peer.Get("key-0004"); !e.Deleted {
		t.Fatalf("local tombstone missing on peer: %+v", e)
	}
	if !rootsEqual(p.local, p.peer) {
		t.Fatal("roots not equal after bidirectional sync")
	}

	// Reverse direction must be a no-op round.
	rev := NewSyncer(DirectLocal{St: p.peer}, NewClient(p.localURL))
	r2, err := rev.Sync(p.ctx)
	if err != nil || !r2.Converged || r2.NodeHashesCompared != 0 {
		t.Fatalf("reverse sync not a no-op: %+v err=%v", r2, err)
	}
}

func TestSyncRetriesWhenPeerMutatesMidScan(t *testing.T) {
	p := newTestPair(t)
	seed(p.ctx, NewClient(p.localURL), 200)
	seed(p.ctx, NewClient(p.peerURL), 200)
	pc := NewClient(p.peerURL)
	_, _ = pc.Put(p.ctx, "key-0001", "early", 2)
	// Fire a second mutation before read #1 = first POST /nodes.
	if err := pc.ArmChaos(p.ctx, 0, "put", "key-0002", "late", 2); err != nil {
		t.Fatal(err)
	}

	res, err := p.syncer().Sync(p.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Converged {
		t.Fatalf("did not converge: %+v", res)
	}
	if res.Conflicts != 1 || res.Attempts != 2 {
		t.Fatalf("want exactly one conflict + one retry, got %+v", res)
	}
	if e, _ := p.local.Get("key-0001"); e.Value != "early" {
		t.Fatalf("early update lost: %+v", e)
	}
	if e, _ := p.local.Get("key-0002"); e.Value != "late" {
		t.Fatalf("mid-scan update lost after retry: %+v", e)
	}
	if !rootsEqual(p.local, p.peer) {
		t.Fatal("roots not equal after retried sync")
	}
}

func TestSyncRetriesWhenTombstoneDuringScan(t *testing.T) {
	p := newTestPair(t)
	seed(p.ctx, NewClient(p.localURL), 200)
	seed(p.ctx, NewClient(p.peerURL), 200)
	pc := NewClient(p.peerURL)
	// Pre-existing divergence ensures the first round is scanning (not
	// short-circuiting on equal roots); the chaos mutation then lands
	// mid-scan and must produce a 409 + retry.
	_, _ = pc.Put(p.ctx, "key-0001", "early", 2)
	if err := pc.ArmChaos(p.ctx, 0, "delete", "key-0099", "", 2); err != nil {
		t.Fatal(err)
	}
	res, err := p.syncer().Sync(p.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Converged || res.Conflicts != 1 {
		t.Fatalf("stats: %+v", res)
	}
	if e, _ := p.local.Get("key-0001"); e.Value != "early" {
		t.Fatalf("pre-existing update lost: %+v", e)
	}
	if e, _ := p.local.Get("key-0099"); !e.Deleted || e.Version != 2 {
		t.Fatalf("scan-time tombstone not delivered: %+v", e)
	}
}

func TestRevisionConflictReportedToClient(t *testing.T) {
	peer := store.New()
	srv := httptest.NewServer(NewServer(peer).Handler())
	defer srv.Close()
	c := NewClient(srv.URL)
	ctx := context.Background()
	root, err := c.Root(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.Put(ctx, "k", "v", 1); err != nil {
		t.Fatal(err)
	}
	_, err = c.Nodes(ctx, root.Revision, 0, []int{0})
	if err == nil {
		t.Fatal("want conflict error after peer mutated")
	}
	var conflict *ErrConflict
	if !errors.As(err, &conflict) {
		t.Fatalf("want *ErrConflict, got %T: %v", err, err)
	}
	if conflict.CurrentRevision != 1 {
		t.Fatalf("current revision = %d, want 1", conflict.CurrentRevision)
	}
}

func TestStalePeerVersionNeverOverwritesNewer(t *testing.T) {
	p := newTestPair(t)
	seed(p.ctx, NewClient(p.localURL), 10)
	seed(p.ctx, NewClient(p.peerURL), 10)
	// Local races ahead to v3. Sync must propagate v3 to the peer and never
	// let the peer's older v1 roll local state back.
	if _, err := NewClient(p.localURL).Put(p.ctx, "key-0001", "local-v3", 3); err != nil {
		t.Fatal(err)
	}
	res, err := p.syncer().Sync(p.ctx)
	if err != nil || !res.Converged {
		t.Fatalf("sync: %+v err=%v", res, err)
	}
	if e, _ := p.peer.Get("key-0001"); e.Value != "local-v3" || e.Version != 3 {
		t.Fatalf("newer local value was not propagated: %+v", e)
	}
	if e, _ := p.local.Get("key-0001"); e.Value != "local-v3" {
		t.Fatalf("local newer value was overwritten: %+v", e)
	}
}

func TestWireCountersShowSparseTransfer(t *testing.T) {
	p := newTestPair(t)
	seed(p.ctx, NewClient(p.localURL), 200)
	seed(p.ctx, NewClient(p.peerURL), 200)
	pc := NewClient(p.peerURL)
	_, _ = pc.Put(p.ctx, "key-0007", "z", 2)

	sy := p.syncer()
	if _, err := sy.Sync(p.ctx); err != nil {
		t.Fatal(err)
	}
	w := sy.WireStats
	if w.ValuesRecv != 1 {
		t.Fatalf("want exactly 1 value transferred, got %d (%+v)", w.ValuesRecv, w)
	}
	// A full snapshot of 200 entries is thousands of bytes; sparse round
	// should be a small fraction.
	snap, err := NewClient(p.peerURL).Snapshot(p.ctx)
	if err != nil {
		t.Fatal(err)
	}
	full := 0
	for _, e := range snap.Entries {
		full += len(e.Key) + len(e.Value)
	}
	if w.BytesReceived >= full/2 {
		t.Fatalf("sparse round received %d bytes, full payload ~%d", w.BytesReceived, full)
	}
}
