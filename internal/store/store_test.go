package store

import (
	"context"
	"errors"
	"fmt"
	"math/rand"
	"sort"
	"sync"
	"testing"
	"time"

	"github.com/cockroachdb/pebble"
)

func testOptions() Options {
	return Options{
		KeepSnapshots: 3,
		ReaderTTL:     5 * time.Second,
		Logger:        func(string, ...any) {}, // quiet in tests
	}
}

// replay recomputes the reference state at height by applying every delta
// from genesis — the ground truth all snapshot-based reads are checked against.
func replay(t *testing.T, blocks []map[string]int64, height uint64) map[string]int64 {
	t.Helper()
	state := map[string]int64{}
	for h := uint64(1); h <= height; h++ {
		applyChanges(state, blocks[h-1])
	}
	return state
}

func randomBlocks(r *rand.Rand, n, accounts int) []map[string]int64 {
	blocks := make([]map[string]int64, n)
	for i := range blocks {
		changes := map[string]int64{}
		for j := 0; j < 1+r.Intn(5); j++ {
			acct := fmt.Sprintf("acct-%03d", r.Intn(accounts))
			changes[acct] = int64(r.Intn(200) - 100)
		}
		blocks[i] = changes
	}
	return blocks
}

func commitBlocks(t *testing.T, s *Store, blocks []map[string]int64) {
	t.Helper()
	for i, b := range blocks {
		h, err := s.AppendBlock(b)
		if err != nil {
			t.Fatalf("append block %d: %v", i+1, err)
		}
		if h != uint64(i+1) {
			t.Fatalf("height = %d, want %d", h, i+1)
		}
	}
}

func checkSummary(t *testing.T, s *Store, blocks []map[string]int64, h uint64) {
	t.Helper()
	sum, err := s.Summary(context.Background(), h, 0)
	if err != nil {
		t.Fatalf("summary at %d: %v", h, err)
	}
	ref := replay(t, blocks, h)
	if sum.Accounts != len(ref) {
		t.Fatalf("height %d: accounts = %d, replay %d", h, sum.Accounts, len(ref))
	}
	if sum.TotalBalance != totalBalance(ref) {
		t.Fatalf("height %d: total = %d, replay %d", h, sum.TotalBalance, totalBalance(ref))
	}
	if sum.StateHash != hashState(ref) {
		t.Fatalf("height %d: state hash mismatch with replay", h)
	}
}

// TestReplayConsistency: snapshot+delta reconstruction must equal full
// replay from genesis at every height.
func TestReplayConsistency(t *testing.T) {
	r := rand.New(rand.NewSource(42))
	blocks := randomBlocks(r, 60, 25)

	opts := testOptions()
	opts.KeepSnapshots = 100 // no pruning in this test
	s, err := Open(t.TempDir(), opts)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	commitBlocks(t, s, blocks)
	for _, h := range []uint64{15, 30, 45, 60} {
		if _, err := s.BuildSnapshot(context.Background(), h); err != nil {
			t.Fatalf("build snapshot %d: %v", h, err)
		}
	}
	for h := uint64(1); h <= 60; h++ {
		checkSummary(t, s, blocks, h)
	}
}

// TestConcurrentQueriesDuringSnapshot: historical queries racing snapshot
// construction and new block writes must all match replay.
func TestConcurrentQueriesDuringSnapshot(t *testing.T) {
	r := rand.New(rand.NewSource(7))
	blocks := randomBlocks(r, 120, 40)

	opts := testOptions()
	opts.KeepSnapshots = 100
	s, err := Open(t.TempDir(), opts)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	commitBlocks(t, s, blocks[:60])

	var readersWG, workersWG sync.WaitGroup
	errCh := make(chan error, 64)

	// Readers: hammer historical queries while the world changes.
	stop := make(chan struct{})
	for i := 0; i < 8; i++ {
		readersWG.Add(1)
		go func(seed int64) {
			defer readersWG.Done()
			rr := rand.New(rand.NewSource(seed))
			for {
				select {
				case <-stop:
					return
				default:
				}
				h := uint64(1 + rr.Intn(60))
				sum, err := s.Summary(context.Background(), h, 0)
				if err != nil {
					errCh <- fmt.Errorf("summary %d: %w", h, err)
					return
				}
				ref := replay(t, blocks, h)
				if sum.StateHash != hashState(ref) {
					errCh <- fmt.Errorf("height %d: hash mismatch under concurrency", h)
					return
				}
			}
		}(int64(i * 977))
	}

	// Builder: construct snapshots while readers run.
	workersWG.Add(1)
	go func() {
		defer workersWG.Done()
		for _, h := range []uint64{20, 40, 60} {
			if _, err := s.BuildSnapshot(context.Background(), h); err != nil {
				errCh <- fmt.Errorf("build %d: %w", h, err)
				return
			}
		}
	}()

	// Writer: new blocks keep landing during snapshot construction.
	workersWG.Add(1)
	go func() {
		defer workersWG.Done()
		for _, b := range blocks[60:] {
			if _, err := s.AppendBlock(b); err != nil {
				errCh <- fmt.Errorf("append: %w", err)
				return
			}
		}
	}()

	workersWG.Wait()
	close(stop)
	readersWG.Wait()
	select {
	case err := <-errCh:
		t.Fatal(err)
	default:
	}
	if got := s.Head(); got != 120 {
		t.Fatalf("head = %d, want 120", got)
	}
	// Post-quiesce: every height still matches replay.
	for h := uint64(1); h <= 120; h++ {
		checkSummary(t, s, blocks, h)
	}
}

// TestSnapshotInterruption: a crash mid-build leaves a temporary snapshot
// that recovery removes and never lists.
func TestSnapshotInterruption(t *testing.T) {
	dir := t.TempDir()
	r := rand.New(rand.NewSource(1))
	blocks := randomBlocks(r, 20, 30)

	opts := testOptions()
	crashAfter := 5
	calls := 0
	opts.BuildHook = func(written int) error {
		calls++
		if written >= crashAfter {
			return errors.New("simulated crash")
		}
		return nil
	}
	s, err := Open(dir, opts)
	if err != nil {
		t.Fatal(err)
	}
	commitBlocks(t, s, blocks)

	if _, err := s.BuildSnapshot(context.Background(), 20); err == nil {
		t.Fatal("expected build to abort")
	}
	if calls == 0 {
		t.Fatal("build hook never ran")
	}
	// Temporary snapshot data exists on disk but is not committed.
	if got := s.ListSnapshots(); len(got) != 0 {
		t.Fatalf("interrupted snapshot listed: %+v", got)
	}
	s.Close()

	// Reopen: recovery must remove the orphan and not list it.
	s2, err := Open(dir, testOptions())
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	if got := s2.ListSnapshots(); len(got) != 0 {
		t.Fatalf("temporary snapshot listed after recovery: %+v", got)
	}
	orphans, err := s2.scanSnapshotHeights()
	if err != nil {
		t.Fatal(err)
	}
	if len(orphans) != 0 {
		t.Fatalf("orphan snapshot keys survived recovery at heights %v", orphans)
	}
	// And a fresh build at the same height works.
	if _, err := s2.BuildSnapshot(context.Background(), 20); err != nil {
		t.Fatalf("rebuild after recovery: %v", err)
	}
	if got := s2.ListSnapshots(); len(got) != 1 || got[0].Height != 20 {
		t.Fatalf("snapshots after rebuild: %+v", got)
	}
}

// TestMissingSealNotListed: a manifest entry whose seal file is gone must be
// dropped by recovery — never served as available.
func TestMissingSealNotListed(t *testing.T) {
	dir := t.TempDir()
	blocks := randomBlocks(rand.New(rand.NewSource(2)), 10, 10)

	s, err := Open(dir, testOptions())
	if err != nil {
		t.Fatal(err)
	}
	commitBlocks(t, s, blocks)
	if _, err := s.BuildSnapshot(context.Background(), 10); err != nil {
		t.Fatal(err)
	}
	s.Close()

	// Corrupt the database: delete the seal behind the store's back.
	db, err := pebble.Open(dir, &pebble.Options{})
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Delete([]byte(snapSealKey(10)), pebble.Sync); err != nil {
		t.Fatal(err)
	}
	db.Close()

	s2, err := Open(dir, testOptions())
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	if got := s2.ListSnapshots(); len(got) != 0 {
		t.Fatalf("snapshot with missing seal listed: %+v", got)
	}
}

// TestCorruptSnapshotDataNotListed: a snapshot whose data keys were damaged
// (seal no longer matches) must be dropped by recovery.
func TestCorruptSnapshotDataNotListed(t *testing.T) {
	dir := t.TempDir()
	blocks := randomBlocks(rand.New(rand.NewSource(3)), 10, 10)

	s, err := Open(dir, testOptions())
	if err != nil {
		t.Fatal(err)
	}
	commitBlocks(t, s, blocks)
	meta, err := s.BuildSnapshot(context.Background(), 10)
	if err != nil {
		t.Fatal(err)
	}
	s.Close()

	db, err := pebble.Open(dir, &pebble.Options{})
	if err != nil {
		t.Fatal(err)
	}
	// Delete one account key from the snapshot data.
	prefix := snapDataPrefix(meta.Height)
	it, _ := db.NewIter(&pebble.IterOptions{
		LowerBound: []byte(prefix),
		UpperBound: []byte(prefixUpper(prefix)),
	})
	it.First()
	victim := append([]byte{}, it.Key()...)
	it.Close()
	if err := db.Delete(victim, pebble.Sync); err != nil {
		t.Fatal(err)
	}
	db.Close()

	s2, err := Open(dir, testOptions())
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	if got := s2.ListSnapshots(); len(got) != 0 {
		t.Fatalf("corrupt snapshot listed: %+v", got)
	}
}

// TestPruneKeepsThree: pruning retains exactly the 3 newest snapshots and
// deletes the deltas they cover; pruned heights fail explicitly.
func TestPruneKeepsThree(t *testing.T) {
	blocks := randomBlocks(rand.New(rand.NewSource(4)), 50, 20)
	s, err := Open(t.TempDir(), testOptions()) // KeepSnapshots = 3
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	commitBlocks(t, s, blocks)

	for _, h := range []uint64{10, 20, 30, 40, 50} {
		if _, err := s.BuildSnapshot(context.Background(), h); err != nil {
			t.Fatal(err)
		}
	}
	snaps := s.ListSnapshots()
	var heights []uint64
	for _, m := range snaps {
		heights = append(heights, m.Height)
	}
	want := []uint64{30, 40, 50}
	if fmt.Sprint(heights) != fmt.Sprint(want) {
		t.Fatalf("retained snapshots = %v, want %v", heights, want)
	}

	// Heights at or below the oldest retained snapshot's coverage are gone.
	if _, err := s.Summary(context.Background(), 25, 0); !errors.Is(err, ErrHeightPruned) {
		t.Fatalf("summary at pruned height 25: err = %v, want ErrHeightPruned", err)
	}
	// Heights served by retained snapshots still match replay.
	for h := uint64(30); h <= 50; h++ {
		checkSummary(t, s, blocks, h)
	}
}

// TestPruneRespectsActiveReader: pruning must not delete the snapshot or
// deltas an active reader depends on.
func TestPruneRespectsActiveReader(t *testing.T) {
	blocks := randomBlocks(rand.New(rand.NewSource(5)), 30, 15)
	opts := testOptions()
	opts.KeepSnapshots = 1 // aggressive: keep only the newest
	s, err := Open(t.TempDir(), opts)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	commitBlocks(t, s, blocks)

	if _, err := s.BuildSnapshot(context.Background(), 10); err != nil {
		t.Fatal(err)
	}
	// Pin the snapshot at 10 with a long-lived reader lease *before* any
	// further build can prune it.
	lease := s.readers.acquire(10, s.opts.Now().Add(time.Hour))
	defer s.readers.release(lease)

	if _, err := s.BuildSnapshot(context.Background(), 20); err != nil {
		t.Fatal(err)
	}
	if _, err := s.BuildSnapshot(context.Background(), 30); err != nil {
		t.Fatal(err)
	}
	if err := s.Prune(context.Background()); err != nil {
		t.Fatal(err)
	}

	// Snapshot 10 survives because the reader pins it; 20 is collected.
	var heights []uint64
	for _, m := range s.ListSnapshots() {
		heights = append(heights, m.Height)
	}
	sort.Slice(heights, func(i, j int) bool { return heights[i] < heights[j] })
	if fmt.Sprint(heights) != "[10 30]" {
		t.Fatalf("snapshots = %v, want [10 30]", heights)
	}
	// The pinned reader can still reconstruct heights in (10, 30].
	for h := uint64(11); h <= 30; h++ {
		checkSummary(t, s, blocks, h)
	}
	// But heights needing deltas <= 10 are pruned.
	if _, err := s.Summary(context.Background(), 5, 0); !errors.Is(err, ErrHeightPruned) {
		t.Fatalf("summary at 5: err = %v, want ErrHeightPruned", err)
	}
}

// TestReaderTimeout: a reader whose lease expires mid-query fails explicitly.
func TestReaderTimeout(t *testing.T) {
	blocks := randomBlocks(rand.New(rand.NewSource(6)), 20, 10)
	opts := testOptions()
	opts.ApplyHook = func(uint64) { time.Sleep(3 * time.Millisecond) }
	s, err := Open(t.TempDir(), opts)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	commitBlocks(t, s, blocks)

	_, err = s.Summary(context.Background(), 20, time.Millisecond)
	if !errors.Is(err, ErrReaderTimeout) {
		t.Fatalf("err = %v, want ErrReaderTimeout", err)
	}
	// A generous lease succeeds.
	if _, err := s.Summary(context.Background(), 20, 10*time.Second); err != nil {
		t.Fatalf("with generous ttl: %v", err)
	}
}

// TestFutureHeightRejected.
func TestFutureHeightRejected(t *testing.T) {
	s, err := Open(t.TempDir(), testOptions())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	commitBlocks(t, s, randomBlocks(rand.New(rand.NewSource(8)), 5, 5))
	if _, err := s.Summary(context.Background(), 6, 0); !errors.Is(err, ErrHeightInFuture) {
		t.Fatalf("err = %v, want ErrHeightInFuture", err)
	}
}
