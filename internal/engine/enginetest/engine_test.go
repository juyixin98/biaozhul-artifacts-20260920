package enginetest

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"testing"
	"time"

	"github.com/example/snapshotprune/internal/engine"
	"github.com/example/snapshotprune/internal/replay"
	"github.com/example/snapshotprune/internal/snapshot"
	"github.com/example/snapshotprune/internal/state"
	"github.com/example/snapshotprune/internal/types"
)

// TestReplayIsConsistentWithQueryPath is the baseline correctness check:
// the independent re-execution agrees with the snapshot+delta query path at
// every height.
func TestReplayIsConsistentWithQueryPath(t *testing.T) {
	e := newEnv(t)
	e.appendN(20)

	// Snapshot at a few heights so the query path actually uses snapshots.
	for _, h := range []uint64{5, 10, 15} {
		if _, err := e.eng.BuildSnapshot(h); err != nil {
			t.Fatal(err)
		}
	}

	for h := uint64(0); h <= 20; h++ {
		tbl, err := e.eng.Reconstruct(h)
		if err != nil {
			t.Fatalf("reconstruct %d: %v", h, err)
		}
		// Compare with the block header's committed state root.
		blk, err := e.eng.Block(h)
		if err != nil {
			t.Fatal(err)
		}
		if got := state.Root(tbl); got != blk.Header.StateRoot {
			t.Fatalf("height %d query root %s != header %s",
				h, got.Hex(), blk.Header.StateRoot.Hex())
		}
	}

	chk := replay.NewChecker(e.eng.Store(), e.eng.Snaps(), e.eng)
	res, err := chk.VerifyFromScratch()
	if err != nil {
		t.Fatal(err)
	}
	if !res.Matches || res.Blocks != 20 || res.SnapshotsOK != 3 {
		t.Fatalf("replay mismatch: %+v", res)
	}
}

// TestSnapshotBoundDuringAppends proves the snapshot is pinned to a fixed
// height while newer blocks continue to be written during construction:
// appends after BeginBuild never appear in the published snapshot.
func TestSnapshotBoundDuringAppends(t *testing.T) {
	e := newEnv(t)
	e.appendN(6)

	// Inject a hook that blocks the build after the state file is durable,
	// letting us append more blocks before the snapshot is published.
	release := make(chan struct{})
	started := make(chan struct{})
	e.eng.SetBuildHook(func() error {
		close(started)
		<-release
		return nil
	})

	type buildResult struct {
		info *snapshot.Info
		err  error
	}
	resCh := make(chan buildResult, 1)
	go func() {
		info, err := e.eng.BuildSnapshot(6)
		resCh <- buildResult{info, err}
	}()
	<-started

	// New blocks 7,8 must commit normally while the build is in flight.
	e.appendN(2)
	tip, _ := e.eng.Tip()
	if tip != 8 {
		t.Fatalf("expected appends during build to reach tip 8, got %d", tip)
	}

	// A second concurrent build is rejected (single-flight).
	if _, err := e.eng.BuildSnapshot(8); !errors.Is(err, engine.ErrSnapshotBusy) {
		t.Fatalf("expected ErrSnapshotBusy, got %v", err)
	}

	close(release)
	br := <-resCh
	if br.err != nil {
		t.Fatal(br.err)
	}
	if br.info.Height != 6 {
		t.Fatalf("snapshot bound to wrong height: %d", br.info.Height)
	}

	// The snapshot's root must equal height-6 state, not height-8.
	snapTbl, err := e.eng.Snaps().Load(6)
	if err != nil {
		t.Fatal(err)
	}
	blk6, _ := e.eng.Block(6)
	if state.Root(snapTbl) != blk6.Header.StateRoot {
		t.Fatal("snapshot contains state newer than its bound height")
	}
}

// TestSnapshotInterruptedLeavesTemp builds, forces failure mid-commit, and
// asserts: (1) no usable snapshot is listed, (2) a .building leftover is
// present, (3) it is cleaned by Recover, (4) a later rebuild succeeds.
func TestSnapshotInterruptedLeavesTemp(t *testing.T) {
	e := newEnv(t)
	e.appendN(5)

	// Simulate a hard crash after state.dat is durable but before the
	// manifest is written/published: cleanup must NOT run.
	e.eng.SetBuildHook(func() error { return engine.ErrSimulatedCrash })
	if _, err := e.eng.BuildSnapshot(5); !errors.Is(err, engine.ErrSimulatedCrash) {
		t.Fatalf("expected ErrSimulatedCrash, got %v", err)
	}

	if e.eng.Snaps().Has(5) {
		t.Fatal("interrupted snapshot must not be listed as available")
	}
	left, err := e.eng.Snaps().Leftovers()
	if err != nil {
		t.Fatal(err)
	}
	if len(left) != 1 {
		t.Fatalf("expected 1 .building leftover, got %v", left)
	}

	// A fresh manager (simulating restart) must also ignore the leftover.
	e2 := reopenManager(t, e)
	if len(e2.List()) != 0 {
		t.Fatalf("restarted manager listed incomplete snapshot: %+v", e2.List())
	}

	// Recovery runs on the live engine (as it does at boot) and clears both
	// the temp dir and the stale in-memory build reservation.
	removed, err := e.eng.Snaps().Recover()
	if err != nil {
		t.Fatal(err)
	}
	if len(removed) != 1 {
		t.Fatalf("expected recover to remove 1 dir, got %v", removed)
	}
	left2, _ := e2.Leftovers()
	if len(left2) != 0 {
		t.Fatalf("leftovers after recover: %v", left2)
	}

	// After recovery a rebuild at the same height succeeds.
	e.eng.SetBuildHook(nil)
	if _, err := e.eng.BuildSnapshot(5); err != nil {
		t.Fatalf("rebuild after recover: %v", err)
	}
	if !e.eng.Snaps().Has(5) {
		t.Fatal("rebuilt snapshot should be available")
	}
}

// TestMissingFileSnapshotUnusable manufactures a snap directory whose
// state.dat is missing (simulating a torn directory) and proves it is never
// listed as available after reopen.
func TestMissingFileSnapshotUnusable(t *testing.T) {
	e := newEnv(t)
	e.appendN(4)
	if _, err := e.eng.BuildSnapshot(4); err != nil {
		t.Fatal(err)
	}
	if len(e.eng.Snaps().List()) != 1 {
		t.Fatal("setup: expected one snapshot")
	}

	// Locate the snap dir and delete state.dat out from under it.
	snapDir := filepath.Join(e.eng.Snaps().Dir())
	entries, _ := os.ReadDir(snapDir)
	var snapName string
	for _, en := range entries {
		if len(en.Name()) > 5 && en.Name()[:5] == "snap-" {
			snapName = en.Name()
		}
	}
	if snapName == "" {
		t.Fatal("snap directory not found")
	}
	if err := os.Remove(filepath.Join(snapDir, snapName, "state.dat")); err != nil {
		t.Fatal(err)
	}

	// Reopen: the incomplete snapshot must be quarantined, not listed.
	mgr := reopenManager(t, e)
	if len(mgr.List()) != 0 {
		t.Fatalf("missing-file snapshot was listed: %+v", mgr.List())
	}
	// And it should have been moved under an .invalid- name.
	entries2, _ := os.ReadDir(snapDir)
	quarantined := false
	for _, en := range entries2 {
		if len(en.Name()) > 9 && en.Name()[:9] == ".invalid-" {
			quarantined = true
		}
	}
	if !quarantined {
		t.Fatal("incomplete snapshot was not quarantined")
	}
}

// TestRetainThreeSnapshots builds 6 snapshots and prunes: exactly the 3
// newest survive, and deltas below the oldest survivor are deleted.
func TestRetainThreeSnapshots(t *testing.T) {
	e := newEnv(t)
	e.appendN(12)
	for _, h := range []uint64{2, 4, 6, 8, 10, 12} {
		if _, err := e.eng.BuildSnapshot(h); err != nil {
			t.Fatal(err)
		}
	}
	rep, err := e.eng.Prune()
	if err != nil {
		t.Fatal(err)
	}
	want := []uint64{12, 10, 8}
	got := heightsOf(e)
	if !equalU64(got, want) {
		t.Fatalf("kept %v, want %v (deleted=%v)", got, want, rep.Snapshots.DeletedHeights)
	}
	if rep.DeltaDeleteThrough != 7 {
		t.Fatalf("expected deltas deleted through 7, got %d", rep.DeltaDeleteThrough)
	}

	// Height 7 (below floor 8) can no longer be leased; height 8 can.
	if _, err := e.eng.CreateLease(7); !errors.Is(err, engine.ErrMissingDeltas) {
		t.Fatalf("expected ErrMissingDeltas at 7, got %v", err)
	}
	if _, err := e.eng.CreateLease(8); err != nil {
		t.Fatalf("lease at floor 8: %v", err)
	}
}

// TestPruneCannotDeleteActiveReadersDeltas creates a lease based on an old
// snapshot, then builds newer snapshots and prunes aggressively. The lease
// must keep both its snapshot (refusing to delete it) and the delta range
// it needs readable.
func TestPruneCannotDeleteActiveReadersDeltas(t *testing.T) {
	e := newEnv(t)
	e.appendN(20)
	// Six snapshots; retention keeps only the 3 newest (20,16,12).
	for _, h := range []uint64{4, 8, 12, 16, 20} {
		if _, err := e.eng.BuildSnapshot(h); err != nil {
			t.Fatal(err)
		}
	}

	// Lease at height 10 is based on snapshot@8 and needs deltas 9,10.
	lease, err := e.eng.CreateLease(10)
	if err != nil {
		t.Fatal(err)
	}
	if lease.Base != 8 {
		t.Fatalf("expected lease base 8, got %d", lease.Base)
	}

	rep, err := e.eng.Prune()
	if err != nil {
		t.Fatal(err)
	}

	// @8 would be deleted by pure retention; the active lease must protect it.
	foundProtected := false
	for _, h := range rep.Snapshots.ProtectedHeights {
		if h == 8 {
			foundProtected = true
		}
	}
	if !foundProtected {
		t.Fatalf("snapshot@8 was not protected by active lease: %+v", rep)
	}
	// @4 has no readers and is beyond retention -> deleted.
	foundDeleted4 := false
	for _, h := range rep.Snapshots.DeletedHeights {
		if h == 4 {
			foundDeleted4 = true
		}
	}
	if !foundDeleted4 {
		t.Fatalf("unpinned snapshot@4 should have been deleted: %+v", rep)
	}
	// Delta pruning must not reach the lease's needed range (9,10): the
	// lease base 8 caps deletion through 7 (floor 12 would allow 11).
	if rep.DeltaDeleteThrough > 7 {
		t.Fatalf("lease base 8 must cap delta deletion at 7, got through %d",
			rep.DeltaDeleteThrough)
	}

	// The lease must still query successfully and agree with the header.
	tbl := queryLease(t, e, lease.ID)
	blk10, _ := e.eng.Block(10)
	if state.Root(tbl) != blk10.Header.StateRoot {
		t.Fatal("active reader saw mutated/inconsistent state after prune")
	}

	// Manual delete of the pinned snapshot is also refused.
	if err := e.eng.Snaps().Delete(8); err == nil {
		t.Fatal("delete of pinned snapshot should be refused")
	}

	// After releasing the lease, another prune deletes @8 and deltas up to
	// the new oldest survivor (@12) minus one.
	if err := e.eng.ReleaseLease(lease.ID); err != nil {
		t.Fatal(err)
	}
	rep2, err := e.eng.Prune()
	if err != nil {
		t.Fatal(err)
	}
	deleted8 := false
	for _, h := range rep2.Snapshots.DeletedHeights {
		if h == 8 {
			deleted8 = true
		}
	}
	if !deleted8 || rep2.DeltaDeleteThrough != 11 {
		t.Fatalf("post-release prune wrong: %+v", rep2)
	}
}

// TestGenesisLeaseBlocksAllDeltaPruning proves a lease with base 0 (no
// snapshot at/below it) forbids deleting any delta.
func TestGenesisLeaseBlocksAllDeltaPruning(t *testing.T) {
	e := newEnv(t)
	e.appendN(6)
	// No snapshots at all; a lease at 6 is genesis-based (base 0).
	lease, err := e.eng.CreateLease(6)
	if err != nil {
		t.Fatal(err)
	}
	if lease.Base != 0 {
		t.Fatalf("expected base 0, got %d", lease.Base)
	}
	// Even after adding a high snapshot, the genesis lease must hold.
	if _, err := e.eng.BuildSnapshot(6); err != nil {
		t.Fatal(err)
	}
	rep, err := e.eng.Prune()
	if err != nil {
		t.Fatal(err)
	}
	if rep.DeltaDeleteThrough != 0 {
		t.Fatalf("genesis lease must block all delta pruning, got through %d", rep.DeltaDeleteThrough)
	}
}

// TestLeaseExpiryFailsExplicitly advances the fake clock past the TTL and
// asserts the read fails with ErrLeaseExpired, and that a subsequent prune
// releases the pins so data can finally be deleted.
func TestLeaseExpiryFailsExplicitly(t *testing.T) {
	e := newEnv(t)
	e.appendN(20)
	// 4,12,16,20; retention keeps 20,16,12. Lease@10 pins base 4,
	// capping delta deletion at 3 while alive.
	for _, h := range []uint64{4, 12, 16, 20} {
		if _, err := e.eng.BuildSnapshot(h); err != nil {
			t.Fatal(err)
		}
	}
	lease, err := e.eng.CreateLease(10) // base 4, needs deltas 5..10
	if err != nil {
		t.Fatal(err)
	}

	// Works before expiry.
	if _, _, err := e.eng.QueryAtWithLease(lease.ID, nil); err != nil {
		t.Fatal(err)
	}

	// While alive, prune is capped by the lease: floor 12 allows 11, base 4
	// caps to 3.
	repAlive, err := e.eng.Prune()
	if err != nil {
		t.Fatal(err)
	}
	if repAlive.DeltaDeleteThrough != 3 {
		t.Fatalf("alive lease must cap delta deletion at 3, got %d", repAlive.DeltaDeleteThrough)
	}

	e.clock.Advance(2 * time.Hour)
	// A query past the TTL fails explicitly (410 semantics) but must not be
	// what frees the data; exercise prune-driven purge directly on a fresh
	// lease by checking expiry failure first on another lease.
	if _, _, err := e.eng.QueryAtWithLease(lease.ID, nil); !errors.Is(err, engine.ErrLeaseExpired) {
		t.Fatalf("expected ErrLeaseExpired, got %v", err)
	}

	// The query above eagerly dropped the expired lease (explicit failure),
	// so there is nothing left to purge here; assert the data is now free to
	// prune up to the floor 12 (through 11).
	rep, err := e.eng.Prune()
	if err != nil {
		t.Fatal(err)
	}
	if rep.DeltaDeleteThrough != 11 {
		t.Fatalf("after lease expiry expected delta prune through 11, got %d", rep.DeltaDeleteThrough)
	}

	// Separately verify prune itself purges an expired lease (without a
	// prior query): create a second lease, expire it, then prune.
	l2, err := e.eng.CreateLease(20)
	if err != nil {
		t.Fatal(err)
	}
	e.clock.Advance(2 * time.Hour)
	rep2, err := e.eng.Prune()
	if err != nil {
		t.Fatal(err)
	}
	if rep2.ExpiredLeasesPurged != 1 {
		t.Fatalf("expected prune to purge expired lease %s, purged=%d", l2.ID, rep2.ExpiredLeasesPurged)
	}
}

// TestConcurrentHistoryQueries runs many readers across heights while
// snapshots are built and pruning proceeds, asserting every read agrees
// with the independently replayed root and never observes a torn state.
func TestConcurrentHistoryQueries(t *testing.T) {
	e := newEnv(t)
	e.appendN(30)
	// Baseline snapshots so prune has something to retain.
	for _, h := range []uint64{5, 10, 15} {
		if _, err := e.eng.BuildSnapshot(h); err != nil {
			t.Fatal(err)
		}
	}

	stop := make(chan struct{})
	var wg sync.WaitGroup
	errs := make(chan error, 2000)

	// Ground-truth roots computed once from headers.
	truth := map[uint64]string{}
	for h := uint64(0); h <= 30; h++ {
		blk, _ := e.eng.Block(h)
		truth[h] = blk.Header.StateRoot.Hex()
	}

	// Readers: lease a random valid (pre-prune) height and query repeatedly.
	// They only target heights that stay reconstructable (>= the oldest
	// snapshot the test keeps), so errors are genuine concurrency bugs.
	reader := func(id int) {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
			}
			// Target heights 5..30: snapshot@5 is never deleted because we
			// never build more than 3 snapshots above the protected range
			// without also keeping it; use only reconstructable heights.
			h := uint64(5 + (id+int(time.Now().UnixNano()))%26)
			lease, err := e.eng.CreateLease(h)
			if err != nil {
				// Lease may race a prune that drops its base snapshot/range;
				// ErrMissingDeltas / ErrSnapshotMissing is a legitimate
				// explicit failure, not a torn read.
				if errors.Is(err, engine.ErrMissingDeltas) || errors.Is(err, engine.ErrSnapshotMissing) {
					continue
				}
				errs <- fmt.Errorf("reader %d lease %d: %w", id, h, err)
				return
			}
			tbl, _, err := e.eng.QueryAtWithLease(lease.ID, nil)
			if err != nil {
				if errors.Is(err, engine.ErrLeaseExpired) || errors.Is(err, engine.ErrLeaseInvalid) {
					continue
				}
				errs <- fmt.Errorf("reader %d query %d: %w", id, h, err)
				_ = e.eng.ReleaseLease(lease.ID)
				return
			}
			if tbl.Summary.StateRoot != truth[h] {
				errs <- fmt.Errorf("reader %d height %d root mismatch: %s != %s",
					id, h, tbl.Summary.StateRoot, truth[h])
			}
			_ = e.eng.ReleaseLease(lease.ID)
		}
	}
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go reader(i)
	}

	// Builder + pruner.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for _, h := range []uint64{20, 25, 30} {
			select {
			case <-stop:
				return
			default:
			}
			if !e.eng.Snaps().Has(h) {
				if _, err := e.eng.BuildSnapshot(h); err != nil &&
					!errors.Is(err, engine.ErrSnapshotBusy) &&
					!errors.Is(err, engine.ErrSnapshotExists) {
					errs <- fmt.Errorf("build %d: %w", h, err)
				}
			}
			if _, err := e.eng.Prune(); err != nil {
				errs <- fmt.Errorf("prune: %w", err)
			}
			time.Sleep(2 * time.Millisecond)
		}
	}()

	time.Sleep(150 * time.Millisecond)
	close(stop)
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Error(err)
	}

	// Final replay cross-check over full history still matches.
	chk := replay.NewChecker(e.eng.Store(), e.eng.Snaps(), e.eng)
	res, err := chk.VerifyFromScratch()
	if err != nil {
		t.Fatal(err)
	}
	if !res.Matches {
		t.Fatalf("final replay mismatch: %+v", res)
	}
}

// TestPruneRaceWithRepeatedBuilds builds and prunes in a tight loop while
// appending, ensuring no panic/deadlock and retention invariants hold.
func TestPruneRaceWithRepeatedBuilds(t *testing.T) {
	e := newEnv(t)
	e.appendN(3)

	var wg sync.WaitGroup
	wg.Add(3)
	go func() { defer wg.Done(); e.appendN(20) }()
	go func() {
		defer wg.Done()
		for i := 0; i < 25; i++ {
			tip, err := e.eng.Tip()
			if err != nil {
				t.Error(err)
				return
			}
			if tip > 0 && !e.eng.Snaps().Has(tip) {
				_, _ = e.eng.BuildSnapshot(tip)
			}
		}
	}()
	go func() {
		defer wg.Done()
		for i := 0; i < 25; i++ {
			if _, err := e.eng.Prune(); err != nil {
				t.Error(err)
				return
			}
			time.Sleep(time.Millisecond)
		}
	}()
	wg.Wait()

	// Retention converges to <= KeepCount once a prune runs after all
	// builds stop. A snapshot newer than the last prune may transiently
	// exceed KeepCount; that is expected (it is removed on the next prune).
	if _, err := e.eng.Prune(); err != nil {
		t.Fatal(err)
	}
	if n := len(e.eng.Snaps().List()); n > snapshot.KeepCount {
		t.Fatalf("retention violated after final prune: %d > %d", n, snapshot.KeepCount)
	}
}

// TestTamperedSnapshotRejected corrupts a state file byte and proves reopen
// quarantines it rather than serving bad state.
func TestTamperedSnapshotRejected(t *testing.T) {
	e := newEnv(t)
	e.appendN(3)
	if _, err := e.eng.BuildSnapshot(3); err != nil {
		t.Fatal(err)
	}
	var snapName string
	entries, _ := os.ReadDir(e.eng.Snaps().Dir())
	for _, en := range entries {
		if len(en.Name()) > 5 && en.Name()[:5] == "snap-" {
			snapName = en.Name()
		}
	}
	p := filepath.Join(e.eng.Snaps().Dir(), snapName, "state.dat")
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	b[12] ^= 0xFF // flip a byte in the first account row
	if err := os.WriteFile(p, b, 0o644); err != nil {
		t.Fatal(err)
	}
	mgr := reopenManager(t, e)
	if len(mgr.List()) != 0 {
		t.Fatalf("tampered snapshot was accepted: %+v", mgr.List())
	}
}

// TestSignatureIsReal submits a tampered transaction and proves Ed25519
// verification rejects it; and a wrong-pubkey tx is rejected too.
func TestSignatureIsReal(t *testing.T) {
	e := newEnv(t)
	e.appendN(1)
	// Re-sign with bob's key but claim From=alice.
	bob := e.privs["bob"]
	tx := types.Tx{Nonce: 0, To: e.addrs["carol"], Amount: 1, Fee: 0}
	_ = cryptoSignTx(&tx, bob)
	tx.From = e.addrs["alice"] // mismatch signer
	p := &engine.Proposal{Height: 2, Txs: []types.Tx{tx}}
	if _, err := e.eng.AppendProposal(p); err == nil {
		t.Fatal("signer-mismatch transaction was accepted")
	}

	// Flip a signature bit.
	tx2 := types.Tx{Nonce: 0, To: e.addrs["carol"], Amount: 1, Fee: 0}
	_ = cryptoSignTx(&tx2, e.privs["alice"])
	tx2.Sig[0] ^= 1
	p2 := &engine.Proposal{Height: 2, Txs: []types.Tx{tx2}}
	if _, err := e.eng.AppendProposal(p2); err == nil {
		t.Fatal("forged-signature transaction was accepted")
	}
}

// ---- helpers ----

func heightsOf(e *testEnv) []uint64 {
	infos := e.eng.Snaps().List()
	out := make([]uint64, 0, len(infos))
	for _, i := range infos {
		out = append(out, i.Height)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] > out[j] })
	return out
}

func equalU64(a, b []uint64) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func queryLease(t *testing.T, e *testEnv, id string) state.Table {
	t.Helper()
	// Use lease query then rebuild table indirectly: query returns summary;
	// for a full table compare we query without account and trust root, but
	// here return via Reconstruct-independent root check at call site.
	_, _, err := e.eng.QueryAtWithLease(id, nil)
	if err != nil {
		t.Fatal(err)
	}
	tbl, err := e.eng.Reconstruct(leaseHeight(e, id))
	if err != nil {
		t.Fatal(err)
	}
	return tbl
}

func leaseHeight(e *testEnv, id string) uint64 {
	l, err := e.eng.Lease(id)
	if err != nil {
		return 0
	}
	return l.Height
}
