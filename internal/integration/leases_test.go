package integration_test

import (
	"image/color"
	"testing"
	"time"

	"vfxqueue/internal/testsupport"

	"github.com/google/uuid"
)

// TestLeaseCompetition: with 2 concurrent workers and many frames, every frame
// is claimed exactly once and rendered with attempts == 1. SKIP LOCKED must
// never hand the same frame to two workers.
func TestLeaseCompetition(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	a := h.CreateAsset(p, owner, "a.png", testsupport.SolidPNG(4, 4, color.NRGBA{7, 8, 9, 255}))

	var tasks []uuid.UUID
	for i := 0; i < 4; i++ {
		comp, ver := h.FreezeVersion(p, owner, specFromAsset(a.ID.String(), 4, 4, 5))
		task := h.Enqueue(p, comp, ver, 0, 4, 5, owner)
		tasks = append(tasks, task.ID)
	}

	stop := startWorker(h)
	defer stop()
	for _, id := range tasks {
		h.WaitForTask(id, 20*time.Second, "succeeded")
	}
	for _, id := range tasks {
		frames, err := h.Q.ListFrames(h.Ctx, id)
		if err != nil {
			t.Fatal(err)
		}
		for _, f := range frames {
			if f.Status != "succeeded" {
				t.Fatalf("frame %s/%d status=%s", id, f.FrameIndex, f.Status)
			}
			if f.Attempts != 1 {
				t.Fatalf("frame %s/%d attempts=%d, want 1 (double claim?)", id, f.FrameIndex, f.Attempts)
			}
		}
	}
}

// TestLeaseExpiryReclaim: a worker claims a frame then dies without
// heartbeating; after the lease expires another worker claims it (generation
// increments), renders and succeeds.
func TestLeaseExpiryReclaim(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	a := h.CreateAsset(p, owner, "a.png", testsupport.SolidPNG(4, 4, color.NRGBA{1, 2, 3, 255}))
	comp, ver := h.FreezeVersion(p, owner, specFromAsset(a.ID.String(), 4, 4, 1))
	task := h.Enqueue(p, comp, ver, 0, 0, 5, owner)

	token := uuid.New()
	row, err := h.ClaimOnce(token, "dead-worker")
	if err != nil {
		t.Fatal(err)
	}
	firstGen := row.Generation

	// No heartbeat; wait past TTL, then start the real worker.
	time.Sleep(h.Config().LeaseTTL + 250*time.Millisecond)
	stop := startWorker(h)
	defer stop()
	h.WaitForTask(task.ID, 15*time.Second, "succeeded")

	ff, err := h.Q.GetFrame(h.Ctx, row.FrameID)
	if err != nil {
		t.Fatal(err)
	}
	if ff.Status != "succeeded" {
		t.Fatalf("status=%s", ff.Status)
	}
	if ff.Generation <= firstGen {
		t.Fatalf("generation did not advance on reclaim: %d -> %d", firstGen, ff.Generation)
	}
}

// TestLateSubmitDoesNotOverwrite: the original stale lease holder submits a
// result after the frame was reclaimed by a new generation. The stale commit
// is rejected and cannot overwrite the new output.
func TestLateSubmitDoesNotOverwrite(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	a := h.CreateAsset(p, owner, "a.png", testsupport.SolidPNG(4, 4, color.NRGBA{4, 5, 6, 255}))
	comp, ver := h.FreezeVersion(p, owner, specFromAsset(a.ID.String(), 4, 4, 1))
	task := h.Enqueue(p, comp, ver, 0, 0, 5, owner)

	oldToken := uuid.New()
	row, err := h.ClaimOnce(oldToken, "old-worker")
	if err != nil {
		t.Fatal(err)
	}
	oldGen := row.Generation

	time.Sleep(h.Config().LeaseTTL + 250*time.Millisecond)
	newToken := uuid.New()
	row2, err := h.ClaimOnce(newToken, "new-worker")
	if err != nil {
		t.Fatal(err)
	}
	if row2.FrameID != row.FrameID {
		t.Fatalf("expected reclaim of same frame, got %s vs %s", row2.FrameID, row.FrameID)
	}
	if row2.Generation <= oldGen {
		t.Fatalf("new generation not advanced: %d -> %d", oldGen, row2.Generation)
	}

	// New generation commits a real success.
	newPath, newSum, newSize := h.RenderVersionFrame(ver, 0)
	if !h.SubmitSuccess(row2.FrameID, newToken, row2.Generation, newPath, newSum, newSize) {
		t.Fatal("current generation submit should succeed")
	}

	// Old worker turns up late and tries to commit under stale token/gen.
	latePath, lateSum, lateSize := h.RenderVersionFrame(ver, 0)
	if h.SubmitSuccess(row.FrameID, oldToken, oldGen, latePath, lateSum, lateSize) {
		t.Fatal("late stale submit must NOT be accepted")
	}

	ff, err := h.Q.GetFrame(h.Ctx, row.FrameID)
	if err != nil {
		t.Fatal(err)
	}
	if ff.Status != "succeeded" || ff.Generation != row2.Generation {
		t.Fatalf("frame = status %s gen %d, want succeeded gen %d", ff.Status, ff.Generation, row2.Generation)
	}
	if ff.OutputPath == nil || *ff.OutputPath != newPath {
		t.Fatal("stale worker overwrote the output path")
	}
	_ = task
}
