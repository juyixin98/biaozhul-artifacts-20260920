package syncapi

import (
	"context"
	"fmt"
	"net/http/httptest"
	"testing"

	"merklesync/internal/store"
)

// A writer commits during every scan round for three rounds; sync must keep
// restarting from a fresh /root and still converge once the writer stops.
func TestSyncConvergesAcrossMultipleScanTimeWrites(t *testing.T) {
	local, peer := store.New(), store.New()
	ls := httptest.NewServer(NewServer(local).Handler())
	ps := httptest.NewServer(NewServer(peer).Handler())
	defer ls.Close()
	defer ps.Close()
	ctx := context.Background()

	seed(ctx, NewClient(ls.URL), 200)
	seed(ctx, NewClient(ps.URL), 200)

	// Pre-existing divergence so the first round actually scans rather
	// than short-circuiting on equal roots.
	if _, err := NewClient(ps.URL).Put(ctx, "key-0001", "early", 2); err != nil {
		t.Fatal(err)
	}
	// Fires before the first /nodes of round 1, then re-arms for two more
	// rounds (repeatLeft=2): three conflicts in total, then the fourth
	// round scans a quiet peer and converges.
	if err := NewClient(ps.URL).ArmChaosRepeating(ctx, 0, "put", "key-0005", "round", 2, 2); err != nil {
		t.Fatal(err)
	}

	res, err := NewSyncer(DirectLocal{St: local}, NewClient(ps.URL)).Sync(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Converged {
		t.Fatalf("did not converge: %+v", res)
	}
	if res.Conflicts != 3 || res.Attempts != 4 {
		t.Fatalf("want 3 conflicts over 4 attempts, got conflicts=%d attempts=%d", res.Conflicts, res.Attempts)
	}
	// Three fires total (initial + repeat=2): v2 -> v3 -> v4, with "!"
	// appended on the second and third fire.
	e, _ := local.Get("key-0005")
	if e.Version != 4 || e.Value != "round!!" {
		t.Fatalf("final value of repeatedly mutated key wrong: %+v", e)
	}
}

// Completely disjoint key spaces: every involved bucket differs, all values
// must flow in one bidirectional round.
func TestSyncCompletelyDisjointStores(t *testing.T) {
	p := newTestPair(t)
	lc, pc := NewClient(p.localURL), NewClient(p.peerURL)
	for i := 0; i < 60; i++ {
		_, _ = lc.Put(p.ctx, fmt.Sprintf("local-%03d", i), "L", 1)
		_, _ = pc.Put(p.ctx, fmt.Sprintf("peer-%03d", i), "P", 1)
	}
	res, err := p.syncer().Sync(p.ctx)
	if err != nil || !res.Converged {
		t.Fatalf("res=%+v err=%v", res, err)
	}
	if res.DifferingBuckets == 0 {
		t.Fatal("expected differing buckets")
	}
	for i := 0; i < 60; i++ {
		if e, ok := p.local.Get(fmt.Sprintf("peer-%03d", i)); !ok || e.Value != "P" {
			t.Fatalf("peer key %d missing locally", i)
		}
		if e, ok := p.peer.Get(fmt.Sprintf("local-%03d", i)); !ok || e.Value != "L" {
			t.Fatalf("local key %d missing on peer", i)
		}
	}
}
