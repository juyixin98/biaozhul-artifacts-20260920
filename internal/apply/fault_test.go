package apply

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

// recoverableStages are all stages at which a RECOVERABLE injected failure
// must leave the old artifact fully usable and a later re-apply must succeed.
var recoverableStages = []string{
	StageCheckOld,
	StageSpace,
	StagePrepare,
	StageWriteDelta,
	StageSync,
	StageVerifyDelta,
	StageRename, // a recoverable "failure" right before rename must not switch
	StageFsyncDir,
	StageVerifyNew,
}

func TestRecoverableFaultAtEveryStage(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "art.bin")
	oldB := bytes.Repeat([]byte("oldbase-"), 2000)
	newB := bytes.Repeat([]byte("newart-"), 2200)

	for _, stage := range recoverableStages {
		t.Run(stage, func(t *testing.T) {
			writeTarget(t, target, oldB)
			p := makePatch(t, oldB, newB, 256)

			opts := Options{
				Space: fakeSpace(1 << 30),
				Fault: FaultPolicy{FailStage: stage, FailAfterBytes: 1234},
			}
			f, _ := os.Open(target)
			_, err := Apply(p, f, target, opts)
			f.Close()
			if !errors.Is(err, ErrInjected) {
				t.Fatalf("stage %s: want ErrInjected, got %v", stage, err)
			}

			// What state is consistent depends on whether the fault fired
			// before or after the atomic switch:
			//  - pre-rename stages: target must be the untouched old base
			//  - post-rename stages (fsync-dir, verify-new): target is already
			//    the new artifact (the switch happened); retry is idempotent.
			postRename := stage == StageFsyncDir || stage == StageVerifyNew
			got, rerr := os.ReadFile(target)
			if rerr != nil {
				t.Fatalf("stage %s: target unreadable after fault: %v", stage, rerr)
			}
			if postRename {
				if !bytes.Equal(got, newB) {
					t.Fatalf("stage %s: post-rename fault did not leave new content", stage)
				}
			} else {
				if !bytes.Equal(got, oldB) {
					t.Fatalf("stage %s: old artifact changed after pre-rename fault", stage)
				}
				if _, serr := os.Stat(target + ".delta.tmp"); !os.IsNotExist(serr) {
					t.Fatalf("stage %s: temp left behind after recoverable fault", stage)
				}
			}

			// Retry with no fault must converge to newB and verify NewSum —
			// either by applying (pre-rename faults) or reporting
			// already-applied (post-rename faults).
			p2 := makePatch(t, oldB, newB, 256)
			f2, _ := os.Open(target)
			res, err2 := Apply(p2, f2, target, Options{Space: fakeSpace(1 << 30)})
			f2.Close()
			if err2 != nil && !AlreadyApplied(err2) {
				t.Fatalf("stage %s: retry failed: %v", stage, err2)
			}
			final, _ := os.ReadFile(target)
			if !bytes.Equal(final, newB) {
				t.Fatalf("stage %s: final content wrong", stage)
			}
			if res != nil && res.NewDigest != sha(newB) {
				t.Fatalf("stage %s: final digest wrong", stage)
			}
			// Reset for next stage iteration.
			writeTarget(t, target, oldB)
		})
	}
}

// TestWriteDeltaPartialFaultLeavesOldUsable: fault midway through writing the
// reconstructed temp; old artifact must be unaffected and temp cleaned up.
func TestWriteDeltaPartialFaultLeavesOldUsable(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "art.bin")
	oldB := bytes.Repeat([]byte("base"), 8192)
	newB := bytes.Repeat([]byte("next"), 16384)
	writeTarget(t, target, oldB)
	p := makePatch(t, oldB, newB, 512)

	f, _ := os.Open(target)
	_, err := Apply(p, f, target, Options{
		Space: fakeSpace(1 << 30),
		Fault: FaultPolicy{FailStage: StageWriteDelta, FailAfterBytes: 5000},
	})
	f.Close()
	if !errors.Is(err, ErrInjected) {
		t.Fatalf("got %v", err)
	}
	got, _ := os.ReadFile(target)
	if !bytes.Equal(got, oldB) {
		t.Fatal("old artifact changed after partial write fault")
	}
	if entries, _ := os.ReadDir(dir); len(entries) != 1 {
		names := []string{}
		for _, e := range entries {
			names = append(names, e.Name())
		}
		t.Fatalf("work dir should contain only the target, got %v", names)
	}
}

// TestStaleTempFromPriorCrashIsCleared: simulate a leftover temp file (as a
// hard crash would leave); a later apply must remove it and complete.
func TestStaleTempFromPriorCrashIsCleared(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "art.bin")
	oldB := bytes.Repeat([]byte("old-"), 3000)
	newB := bytes.Repeat([]byte("new-"), 3000)
	writeTarget(t, target, oldB)
	if err := os.WriteFile(target+".delta.tmp", []byte("partial garbage from crash"), 0o644); err != nil {
		t.Fatal(err)
	}
	p := makePatch(t, oldB, newB, 256)
	f, _ := os.Open(target)
	res, err := Apply(p, f, target, Options{Space: fakeSpace(1 << 30)})
	f.Close()
	if err != nil {
		t.Fatalf("apply over stale temp failed: %v", err)
	}
	if res.NewDigest != sha(newB) {
		t.Fatal("digest wrong after stale-temp recovery")
	}
}

// TestConcurrentAppliesSerialize: a second apply while the first holds the
// lock gets ErrLocked (simulated by holding lock externally is awkward; we
// instead verify lock acquisition behavior directly).
func TestLockContention(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "a.bin")
	writeTarget(t, target, []byte("x"))
	l1, err := acquireTargetLock(lockPath(target))
	if err != nil {
		t.Fatal(err)
	}
	defer l1.release()
	_, err = acquireTargetLock(lockPath(target))
	if !errors.Is(err, ErrLocked) {
		t.Fatalf("want ErrLocked, got %v", err)
	}
}
