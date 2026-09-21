package jobs_test

import (
	"context"
	"os"
	"path/filepath"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"forensiccore/internal/jobs"
	"forensiccore/internal/models"
	"forensiccore/internal/service"
	"forensiccore/internal/testutil"
)

// newCase writes data as evidence/name and registers it.
func newCase(t *testing.T, env *testutil.Env, ref, name string, data []byte) *models.Case {
	t.Helper()
	if err := os.WriteFile(filepath.Join(env.EvidenceRoot, name), data, 0o644); err != nil {
		t.Fatal(err)
	}
	c, err := env.Service.RegisterCase(service.RegisterRequest{CaseRef: ref, File: name}, "alice")
	if err != nil {
		t.Fatalf("register: %v", err)
	}
	return c
}

func getJob(t *testing.T, env *testutil.Env, jobID uint) models.VerificationJob {
	t.Helper()
	var j models.VerificationJob
	if err := env.DB.First(&j, jobID).Error; err != nil {
		t.Fatal(err)
	}
	return j
}

func waitForTerminal(t *testing.T, env *testutil.Env, jobID uint, timeout time.Duration) models.VerificationJob {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		j := getJob(t, env, jobID)
		switch j.Status {
		case models.JobCompletedMatch, models.JobCompletedMismatch, models.JobFailed:
			return j
		}
		time.Sleep(10 * time.Millisecond)
	}
	j := getJob(t, env, jobID)
	t.Fatalf("job %d did not finish within %v (status=%s)", jobID, timeout, j.Status)
	return j
}

// interruptHook cancels ctx after chunk targetIdx has been committed and parks
// until release is closed, leaving the interrupted job exactly at that
// committed offset (runJob checks ctx at the top of every iteration and only
// after a commit, so no further chunk is processed once parked).
func interruptHook(targetIdx int, cancel context.CancelFunc, fired *atomic.Bool, release <-chan struct{}) jobs.Hook {
	return func(j *models.VerificationJob, idx int) error {
		if idx == targetIdx && fired.CompareAndSwap(false, true) {
			cancel()
			<-release
		}
		return nil
	}
}

func TestCompletedMatchOnUnchangedFile(t *testing.T) {
	env := testutil.New(t, 32, nil)
	data := []byte("the quick brown fox jumps over the lazy dog, repeatedly!")
	cs := newCase(t, env, "C-OK", "ok.dd", data)
	job, _ := env.Service.StartVerification(cs.ID, "alice", 32)

	if err := env.Runner.RunJobOnce(context.Background(), job.ID); err != nil {
		t.Fatalf("run: %v", err)
	}
	j := waitForTerminal(t, env, job.ID, time.Second)
	if j.Status != models.JobCompletedMatch {
		t.Fatalf("status = %s (%s): %s", j.Status, j.ErrorCode, j.LastError)
	}
	if j.Offset != int64(len(data)) {
		t.Fatalf("offset = %d, want %d", j.Offset, len(data))
	}
}

func TestResumeAfterInterruptSucceeds(t *testing.T) {
	var fired atomic.Bool
	ctx, cancel := context.WithCancel(context.Background())
	release := make(chan struct{})
	env := testutil.New(t, 64, interruptHook(0, cancel, &fired, release))

	data := make([]byte, 256) // 4 chunks
	for i := range data {
		data[i] = byte(i * 7)
	}
	cs := newCase(t, env, "C-RESUME-1", "disk.raw", data)
	job, err := env.Service.StartVerification(cs.ID, "alice", 64)
	if err != nil {
		t.Fatal(err)
	}

	done := make(chan error, 1)
	go func() { done <- env.Runner.RunJobOnce(ctx, job.ID) }()
	waitFired(t, &fired)
	// Parked right after chunk 0 committed (64 bytes); release and join.
	close(release)
	<-done

	j := getJob(t, env, job.ID)
	if j.Offset != 64 || j.Status != models.JobRunning {
		t.Fatalf("after interrupt: offset=%d status=%s, want 64/running", j.Offset, j.Status)
	}
	var chunks int64
	env.DB.Model(&models.JobChunk{}).Where("job_id = ?", job.ID).Count(&chunks)
	if chunks != 1 {
		t.Fatalf("persisted chunks = %d, want 1", chunks)
	}

	// Simulate a process restart: the stale "running" job is requeued and a
	// new runner continues after re-verifying identity + processed prefix.
	env.RestartRunner(t, nil)
	j = waitForTerminal(t, env, job.ID, 3*time.Second)
	if j.Status != models.JobCompletedMatch {
		t.Fatalf("resumed job status = %s (%s): %s", j.Status, j.ErrorCode, j.LastError)
	}
	if j.Offset != 256 {
		t.Fatalf("final offset = %d, want 256", j.Offset)
	}

	res, err := env.Service.VerifyChain(cs.ID)
	if err != nil {
		t.Fatal(err)
	}
	if !res.OK {
		t.Fatalf("chain not OK after resume: %+v", res.Faults)
	}
	if res.EventCount != 2 { // register + verification
		t.Fatalf("event count = %d, want 2", res.EventCount)
	}
}

// mutateFile overwrites bytes at off and restores the original mtime, so the
// prefix-chunk digest check (not the identity check) is what must fire.
func mutateFile(t *testing.T, path string, off int, data []byte) {
	t.Helper()
	fi, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	st := fi.Sys().(*syscall.Stat_t)
	w, err := os.OpenFile(path, os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := w.WriteAt(data, int64(off)); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	atime := time.Unix(st.Atim.Sec, st.Atim.Nsec)
	mtime := time.Unix(st.Mtim.Sec, st.Mtim.Nsec)
	if err := os.Chtimes(path, atime, mtime); err != nil {
		t.Fatal(err)
	}
}

func TestResumeDetectsPrefixModification(t *testing.T) {
	var fired atomic.Bool
	ctx, cancel := context.WithCancel(context.Background())
	release := make(chan struct{})
	env := testutil.New(t, 64, interruptHook(1, cancel, &fired, release))

	data := make([]byte, 256)
	for i := range data {
		data[i] = byte(i)
	}
	cs := newCase(t, env, "C-TAMPER-PREFIX", "tampered.raw", data)
	job, _ := env.Service.StartVerification(cs.ID, "alice", 64)

	done := make(chan error, 1)
	go func() { done <- env.Runner.RunJobOnce(ctx, job.ID) }()
	waitFired(t, &fired)
	close(release)
	<-done

	j := getJob(t, env, job.ID)
	if j.Offset != 128 {
		t.Fatalf("offset after interrupt = %d, want 128", j.Offset)
	}

	// Modify bytes INSIDE the already-processed prefix while keeping size and
	// mtime. Resume must reject based on chunk content, not filename/size.
	mutateFile(t, filepath.Join(env.EvidenceRoot, "tampered.raw"), 10, []byte{0xDE, 0xAD})

	env.RestartRunner(t, nil)
	j = waitForTerminal(t, env, job.ID, 3*time.Second)
	if j.Status != models.JobFailed || j.ErrorCode != models.ErrCodeContentModified {
		t.Fatalf("status=%s code=%s (%s), want failed/CONTENT_MODIFIED", j.Status, j.ErrorCode, j.LastError)
	}
	if j.Offset != 128 {
		t.Fatalf("offset should remain at 128, got %d", j.Offset)
	}
}

func TestResumeDetectsIdentityChangeByMtime(t *testing.T) {
	var fired atomic.Bool
	ctx, cancel := context.WithCancel(context.Background())
	release := make(chan struct{})
	env := testutil.New(t, 64, interruptHook(0, cancel, &fired, release))

	data := []byte("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd")
	cs := newCase(t, env, "C-MTIME", "m.raw", data)
	job, _ := env.Service.StartVerification(cs.ID, "alice", 64)

	done := make(chan error, 1)
	go func() { done <- env.Runner.RunJobOnce(ctx, job.ID) }()
	waitFired(t, &fired)
	close(release)
	<-done

	// Same size and content but a future mtime: object identity must fail.
	future := time.Now().Add(2 * time.Hour)
	if err := os.Chtimes(filepath.Join(env.EvidenceRoot, "m.raw"), future, future); err != nil {
		t.Fatal(err)
	}
	env.RestartRunner(t, nil)
	j := waitForTerminal(t, env, job.ID, 3*time.Second)
	if j.ErrorCode != models.ErrCodeIdentityMismatch {
		t.Fatalf("error code = %q, want %q (status %s: %s)", j.ErrorCode, models.ErrCodeIdentityMismatch, j.Status, j.LastError)
	}
}

func TestDetectsReplacedFileSameSize(t *testing.T) {
	env := testutil.New(t, 64, nil)
	data := make([]byte, 128)
	for i := range data {
		data[i] = byte(i)
	}
	cs := newCase(t, env, "C-SWAP", "swap.raw", data)

	// Replace with different same-length content (fresh mtime).
	other := make([]byte, 128)
	for i := range other {
		other[i] = byte(255 - i)
	}
	if err := os.WriteFile(filepath.Join(env.EvidenceRoot, "swap.raw"), other, 0o644); err != nil {
		t.Fatal(err)
	}
	job, _ := env.Service.StartVerification(cs.ID, "alice", 64)
	if err := env.Runner.RunJobOnce(context.Background(), job.ID); err != nil {
		t.Fatalf("run: %v", err)
	}
	j := waitForTerminal(t, env, job.ID, time.Second)
	if j.Status != models.JobFailed {
		t.Fatalf("status = %s, want failed", j.Status)
	}
	if j.ErrorCode != models.ErrCodeIdentityMismatch && j.ErrorCode != models.ErrCodeBaselineMismatch {
		t.Fatalf("unexpected error code %q: %s", j.ErrorCode, j.LastError)
	}
}

func waitFired(t *testing.T, fired *atomic.Bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if fired.Load() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("interrupt hook never fired")
}
