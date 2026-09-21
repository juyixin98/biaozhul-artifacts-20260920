package review_test

import (
	"context"
	"errors"
	"os"
	"sync"
	"testing"
	"time"

	"forensiccore/internal/domain"
	"forensiccore/internal/evidence"
	"forensiccore/internal/review"
	"forensiccore/internal/testkit"
)

const chunkSize = 1024

func fill(n int, b byte) []byte {
	out := make([]byte, n)
	for i := range out {
		out[i] = b
	}
	return out
}

func registerOne(t *testing.T, env *testkit.Env, name string, data []byte) (caseID, evID, path string) {
	t.Helper()
	caseID = env.CreateCase(t, "rv-case")
	path = env.WriteFile(t, name, data)
	ev, _, err := env.Evidence.Register(context.Background(), evidence.RegisterInput{
		CaseID: caseID, SourcePath: path, Actor: "role:investigator",
	})
	if err != nil {
		t.Fatalf("register: %v", err)
	}
	return caseID, ev.ID, path
}

func waitFor(t *testing.T, env *testkit.Env, jobID string, cond func(*domain.ReviewJob) bool) *domain.ReviewJob {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		job, err := env.Review.Get(context.Background(), jobID)
		if err != nil {
			t.Fatal(err)
		}
		if cond(job) {
			return job
		}
		time.Sleep(5 * time.Millisecond)
	}
	job, _ := env.Review.Get(context.Background(), jobID)
	t.Fatalf("timed out waiting for job %s; final state: %+v", jobID, job)
	return nil
}

// TestReviewHappyPath completes a review and records a match plus chain event.
func TestReviewHappyPath(t *testing.T) {
	env := testkit.NewEnv(t, chunkSize)
	defer env.Shutdown(t)
	data := fill(20*1024, 0x77)
	caseID, evID, _ := registerOne(t, env, "ok.dd", data)

	job, err := env.Review.Start(context.Background(), evID, "role:investigator")
	if err != nil {
		t.Fatal(err)
	}
	done := waitFor(t, env, job.ID, func(j *domain.ReviewJob) bool { return j.Status != domain.ReviewRunning })
	if done.Status != domain.ReviewCompleted || done.Result != "match" {
		t.Fatalf("unexpected job: %+v", done)
	}
	if done.FinalSHA256 == "" || done.FinalSHA256 != done.BaselineSHA256 {
		t.Fatalf("final sha must equal baseline: %+v", done)
	}
	if done.Offset != int64(len(data)) {
		t.Fatalf("offset = %d, want %d", done.Offset, len(data))
	}
	rep, err := env.Chain.Verify(context.Background(), caseID)
	if err != nil || !rep.Intact {
		t.Fatalf("chain after review: %v %+v", err, rep.Issues)
	}
}

// TestReviewInterruptedAndResumed pauses mid-file with progress persisted,
// then resumes and completes with a matching digest.
func TestReviewInterruptedAndResumed(t *testing.T) {
	env := testkit.NewEnv(t, chunkSize)
	defer env.Shutdown(t)
	env.Review.SetFlushEvery(2) // checkpoint every 2 KiB

	data := fill(32*1024, 0x44)
	caseID, evID, _ := registerOne(t, env, "pause.dd", data)

	var mtx sync.Mutex
	paused := false
	env.Review.Hook = func(ctx context.Context, j *domain.ReviewJob, chunkIndex int, offset int64) error {
		mtx.Lock()
		defer mtx.Unlock()
		// Pause once, strictly mid-file and after a checkpoint was written.
		if !paused && offset >= 16*1024 {
			paused = true
			return review.ErrPause
		}
		return nil
	}

	job0, err := env.Review.Start(context.Background(), evID, "role:investigator")
	if err != nil {
		t.Fatal(err)
	}
	first := waitFor(t, env, job0.ID, func(j *domain.ReviewJob) bool {
		return j.Status == domain.ReviewInterrupted
	})
	if first.Offset == 0 || first.Offset >= int64(len(data)) {
		t.Fatalf("pause must happen mid-file, offset=%d size=%d", first.Offset, len(data))
	}
	if first.PrefixSHA256 == "" {
		t.Fatal("prefix digest must be persisted at pause")
	}

	// Clear hook and resume.
	env.Review.Hook = nil
	job1, err := env.Review.Resume(context.Background(), job0.ID, "role:investigator")
	if err != nil {
		t.Fatalf("resume unchanged file: %v", err)
	}
	final := waitFor(t, env, job1.ID, func(j *domain.ReviewJob) bool {
		return j.Status == domain.ReviewCompleted || j.Status == domain.ReviewFailed
	})
	if final.Status != domain.ReviewCompleted || final.Result != "match" {
		t.Fatalf("resumed review should complete with match, got %+v", final)
	}

	rep, _ := env.Chain.Verify(context.Background(), caseID)
	if !rep.Intact {
		t.Fatalf("chain broken after resume: %+v", rep.Issues)
	}
}

// TestReviewResumeRejectsModifiedPrefix rewrites the already-processed region
// (same size) while paused; resume must fail with ErrPrefixMismatch and must
// NOT continue over untrusted bytes.
func TestReviewResumeRejectsModifiedPrefix(t *testing.T) {
	env := testkit.NewEnv(t, chunkSize)
	defer env.Shutdown(t)
	env.Review.SetFlushEvery(2)

	data := fill(32*1024, 0x44)
	_, evID, path := registerOne(t, env, "prefix.dd", data)

	var mtx sync.Mutex
	paused := false
	env.Review.Hook = func(ctx context.Context, j *domain.ReviewJob, chunkIndex int, offset int64) error {
		mtx.Lock()
		defer mtx.Unlock()
		if !paused && offset >= 16*1024 {
			paused = true
			return review.ErrPause
		}
		return nil
	}
	job0, err := env.Review.Start(context.Background(), evID, "role:investigator")
	if err != nil {
		t.Fatal(err)
	}
	first := waitFor(t, env, job0.ID, func(j *domain.ReviewJob) bool {
		return j.Status == domain.ReviewInterrupted
	})

	// Flip bytes INSIDE the processed prefix, preserving size. The file's
	// mtime changes and its bytes differ; name/size are unchanged.
	f, err := os.OpenFile(path, os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteAt([]byte{0x99, 0x88, 0x77}, 1024); err != nil {
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}

	env.Review.Hook = nil
	_, err = env.Review.Resume(context.Background(), first.ID, "role:investigator")
	if !errors.Is(err, review.ErrPrefixMismatch) {
		t.Fatalf("expected ErrPrefixMismatch, got %v", err)
	}
	got, err := env.Review.Get(context.Background(), first.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != domain.ReviewFailed {
		t.Fatalf("job must be marked failed, got %s", got.Status)
	}
}

// TestReviewResumeRejectsReplacedFile replaces the inode at the same path
// (same size) while paused; resume must reject on identity, not name/size.
func TestReviewResumeRejectsReplacedFile(t *testing.T) {
	env := testkit.NewEnv(t, chunkSize)
	defer env.Shutdown(t)
	env.Review.SetFlushEvery(2)

	data := fill(32*1024, 0x44)
	_, evID, path := registerOne(t, env, "replaced.dd", data)

	var mtx sync.Mutex
	paused := false
	env.Review.Hook = func(ctx context.Context, j *domain.ReviewJob, chunkIndex int, offset int64) error {
		mtx.Lock()
		defer mtx.Unlock()
		if !paused && offset >= 16*1024 {
			paused = true
			return review.ErrPause
		}
		return nil
	}
	job0, err := env.Review.Start(context.Background(), evID, "role:investigator")
	if err != nil {
		t.Fatal(err)
	}
	first := waitFor(t, env, job0.ID, func(j *domain.ReviewJob) bool {
		return j.Status == domain.ReviewInterrupted
	})

	// Replace via rename of a same-size temp file at the same name: new inode.
	tmp := path + ".new"
	if err := os.WriteFile(tmp, fill(32*1024, 0x66), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(tmp, path); err != nil {
		t.Fatal(err)
	}

	env.Review.Hook = nil
	_, err = env.Review.Resume(context.Background(), first.ID, "role:investigator")
	if !errors.Is(err, review.ErrIdentityMismatch) && !errors.Is(err, review.ErrPrefixMismatch) {
		t.Fatalf("expected identity/prefix rejection, got %v", err)
	}
}

// TestReviewResumeRejectsSizeChange verifies size-only differences still fail.
func TestReviewResumeRejectsSizeChange(t *testing.T) {
	env := testkit.NewEnv(t, chunkSize)
	defer env.Shutdown(t)
	env.Review.SetFlushEvery(2)

	data := fill(32*1024, 0x44)
	_, evID, path := registerOne(t, env, "grown.dd", data)

	var mtx sync.Mutex
	paused := false
	env.Review.Hook = func(ctx context.Context, j *domain.ReviewJob, chunkIndex int, offset int64) error {
		mtx.Lock()
		defer mtx.Unlock()
		if !paused && offset >= 16*1024 {
			paused = true
			return review.ErrPause
		}
		return nil
	}
	job0, err := env.Review.Start(context.Background(), evID, "role:investigator")
	if err != nil {
		t.Fatal(err)
	}
	first := waitFor(t, env, job0.ID, func(j *domain.ReviewJob) bool {
		return j.Status == domain.ReviewInterrupted
	})

	if err := os.Truncate(path, 40*1024); err != nil {
		t.Fatal(err)
	}
	env.Review.Hook = nil
	_, err = env.Review.Resume(context.Background(), first.ID, "role:investigator")
	if !errors.Is(err, review.ErrSizeChanged) {
		t.Fatalf("expected ErrSizeChanged, got %v", err)
	}
}

// TestReviewDetectsTamperedBaseline verifies that when the on-disk bytes
// differ from the registered baseline but stayed stable during review, the
// job completes with result=mismatch instead of match.
func TestReviewDetectsTamperedBaseline(t *testing.T) {
	env := testkit.NewEnv(t, chunkSize)
	defer env.Shutdown(t)
	data := fill(16*1024, 0x12)
	_, evID, path := registerOne(t, env, "tampered.dd", data)

	// Rewrite the whole image once, stable afterwards (so the review read is
	// internally consistent but disagrees with the stored baseline).
	if err := os.WriteFile(path, fill(16*1024, 0x21), 0o644); err != nil {
		t.Fatal(err)
	}

	job0, err := env.Review.Start(context.Background(), evID, "role:investigator")
	if err != nil {
		t.Fatal(err)
	}
	final := waitFor(t, env, job0.ID, func(j *domain.ReviewJob) bool {
		return j.Status != domain.ReviewRunning
	})
	if final.Status != domain.ReviewCompleted {
		t.Fatalf("stable-but-different file should complete review, got %+v", final)
	}
	if final.Result != "mismatch" || final.FinalSHA256 == final.BaselineSHA256 {
		t.Fatalf("expected mismatch against baseline, got %+v", final)
	}
}

func TestStartRejectsDuplicateActiveJob(t *testing.T) {
	env := testkit.NewEnv(t, chunkSize)
	defer env.Shutdown(t)
	env.Review.SetFlushEvery(1)

	data := make([]byte, 64*1024)
	_, evID, _ := registerOne(t, env, "dup.dd", data)

	block := make(chan struct{})
	env.Review.Hook = func(ctx context.Context, j *domain.ReviewJob, chunkIndex int, offset int64) error {
		<-block
		return nil
	}
	if _, err := env.Review.Start(context.Background(), evID, "role:investigator"); err != nil {
		t.Fatal(err)
	}
	if _, err := env.Review.Start(context.Background(), evID, "role:investigator"); !errors.Is(err, review.ErrAlreadyRunning) {
		t.Fatalf("expected ErrAlreadyRunning, got %v", err)
	}
	close(block)
}

func TestReviewRejectsSymlinkSwap(t *testing.T) {
	env := testkit.NewEnv(t, chunkSize)
	defer env.Shutdown(t)
	data := fill(8*1024, 0x01)
	_, evID, path := registerOne(t, env, "swap.dd", data)

	// After registration, swap the registered name for a symlink that leaves
	// the whitelist; the review must fail to reopen safely.
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	outside := env.WriteOutside(t, "outside.dd", fill(8*1024, 0x02))
	if err := os.Symlink(outside, path); err != nil {
		t.Fatal(err)
	}
	job0, err := env.Review.Start(context.Background(), evID, "role:investigator")
	if err != nil {
		t.Fatal(err)
	}
	final := waitFor(t, env, job0.ID, func(j *domain.ReviewJob) bool {
		return j.Status == domain.ReviewFailed || j.Status == domain.ReviewCompleted
	})
	if final.Status != domain.ReviewFailed {
		t.Fatalf("symlink swap outside whitelist must fail the job, got %+v", final)
	}
}
