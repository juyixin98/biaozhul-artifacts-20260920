package tests_test

import (
	"net/http"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/vfxqueue/renderq/internal/db/dbgen"
	"github.com/vfxqueue/renderq/internal/testutil"
	"github.com/vfxqueue/renderq/internal/worker"
)

// TestRecoveryContinueFromCompletedFrames: complete frame 0, then simulate
// a process death (a frame leased with a future deadline and job running).
// After Recover + restart of the worker, frame 0 is NOT re-rendered and the
// job resumes from frame 1.
func TestRecoveryContinueFromCompletedFrames(t *testing.T) {
	h := testutil.New(t)
	_, _, versionID := seedComposition(t, h, testutil.AliceKey)
	jobID := createJob(t, h, testutil.AliceKey, versionID, 5, 0, 1)

	q := dbgen.New(h.Pool)

	// Worker renders frame 0 fully.
	wk := runWorker(t, h, 60, 1000)
	if _, err := wk.RunOnce(h.Ctx); err != nil {
		t.Fatal(err)
	}
	before, err := q.GetFrame(h.Ctx, frameIDByNo(t, h, jobID, 0))
	if err != nil {
		t.Fatal(err)
	}
	if before.Status != "succeeded" {
		t.Fatalf("frame0 status = %s", before.Status)
	}
	beforeDigest := before.OutputSha256

	// Simulate crash mid-frame-1: it stays leased by the dead worker.
	leased, err := q.ClaimFrame(h.Ctx, dbgen.ClaimFrameParams{
		WorkerID: "dead", LeaseSeconds: 3600,
	})
	if err != nil {
		t.Fatal(err)
	}
	if leased.FrameNo != 1 {
		t.Fatalf("claimed frame %d, want 1", leased.FrameNo)
	}

	// Restart: recover then run a NEW worker instance (simulating a new
	// process with a different identity).
	if err := worker.Recover(h.Ctx, h.Pool, h.Store, discardLogger()); err != nil {
		t.Fatalf("recover: %v", err)
	}
	wk2 := runWorker(t, h, 60, 1000)
	body := driveUntilJob(t, h, wk2, jobID, "succeeded")
	if body["frameCounts"].(map[string]any)["succeeded"].(float64) != 2 {
		t.Fatalf("counts = %v", body["frameCounts"])
	}

	// Frame 0 must be the original output (not re-rendered): same digest
	// and its attempts still 1.
	after0, _ := q.GetFrame(h.Ctx, before.ID)
	if after0.OutputSha256 != beforeDigest {
		t.Fatalf("frame0 was re-rendered: %s -> %s", beforeDigest, after0.OutputSha256)
	}
	if after0.Attempts != 1 {
		t.Fatalf("frame0 attempts = %d, want 1 (continued, not restarted)", after0.Attempts)
	}
}

// TestRecoveryMissingOutputRerendersAndDoesNotClaimWholeSuccess: the job
// reached succeeded in the DB, but one output file is lost on disk. Recover
// must reopen the job, reset the frame and re-render; the job must not be
// presented as succeeded while the file is missing.
func TestRecoveryMissingOutputRerenders(t *testing.T) {
	h := testutil.New(t)
	_, _, versionID := seedComposition(t, h, testutil.AliceKey)
	jobID := createJob(t, h, testutil.AliceKey, versionID, 5, 0, 0)

	wk := runWorker(t, h, 60, 1000)
	body := driveUntilJob(t, h, wk, jobID, "succeeded")
	if body["status"] != "succeeded" {
		t.Fatalf("status = %v", body["status"])
	}

	// Delete the output file as if the commit landed but the rename was
	// lost (crash between DB commit and fsync).
	if err := os.Remove(h.Store.FramePath(jobID.String(), 0)); err != nil {
		t.Fatal(err)
	}

	if err := worker.Recover(h.Ctx, h.Pool, h.Store, discardLogger()); err != nil {
		t.Fatal(err)
	}
	st, body := h.Do("GET", "/jobs/"+jobID.String(), testutil.AdminKey, nil)
	if st != http.StatusOK {
		t.Fatalf("get: %d", st)
	}
	if body["status"] != "queued" {
		t.Fatalf("job status after recover = %v, want queued", body["status"])
	}

	wk2 := runWorker(t, h, 60, 1000)
	body = driveUntilJob(t, h, wk2, jobID, "succeeded")
	if code, raw := h.DoRaw("GET", "/jobs/"+jobID.String()+"/frames/0/png", testutil.AliceKey, "", nil); code != http.StatusOK {
		t.Fatalf("re-rendered frame missing: %d %s", code, raw)
	}
	if body["status"] != "succeeded" {
		t.Fatalf("final status = %v", body["status"])
	}
}

// TestRecoverySweepsStageFiles: a leftover .stage-* file from a crash is
// removed at startup.
func TestRecoverySweepsStageFiles(t *testing.T) {
	h := testutil.New(t)
	_, _, versionID := seedComposition(t, h, testutil.AliceKey)
	jobID := createJob(t, h, testutil.AliceKey, versionID, 5, 0, 0)

	dir := h.Store.JobDir(jobID.String())
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	staged := dir + "/.stage-abcdef"
	if err := os.WriteFile(staged, []byte("partial"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := worker.Recover(h.Ctx, h.Pool, h.Store, discardLogger()); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(staged); !os.IsNotExist(err) {
		t.Fatalf("staged file survived recovery: %v", err)
	}
}

func frameIDByNo(t *testing.T, h *testutil.Harness, jobID uuid.UUID, no int) uuid.UUID {
	t.Helper()
	for _, f := range listFrames(t, h, jobID) {
		if int(f["frameNo"].(float64)) == no {
			return h.AsID(f, "id")
		}
	}
	t.Fatalf("frame %d not found", no)
	return uuid.Nil
}

func driveUntilJob(t *testing.T, h *testutil.Harness, wk *worker.Worker, jobID uuid.UUID, want string) map[string]any {
	t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := wk.RunOnce(h.Ctx); err != nil {
			t.Fatal(err)
		}
		st, body := h.Do("GET", "/jobs/"+jobID.String(), testutil.AdminKey, nil)
		if st != http.StatusOK {
			t.Fatalf("get: %d", st)
		}
		if body["status"] == want {
			return body
		}
	}
	t.Fatalf("job did not reach %s", want)
	return nil
}
