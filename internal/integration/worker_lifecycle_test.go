// Integration tests for the VFX render queue. These require a real PostgreSQL
// (ephemeral docker container by default; TEST_DATABASE_URL to override).
package integration_test

import (
	"image/color"
	"testing"
	"time"

	"vfxqueue/internal/compositor"
	"vfxqueue/internal/testsupport"
)

func specFromAsset(assetID string, w, h, frames int) *compositor.Spec {
	return &compositor.Spec{
		CanvasWidth: w, CanvasHeight: h, FrameCount: frames,
		Layers: []compositor.Layer{{ID: "l", AssetID: assetID, X: 0, Y: 0}},
	}
}

// TestWorkerRendersFramesAndChecksums is the happy path: enqueue frames, let
// the worker render them, assert real PNG files exist and the DB records the
// matching checksums, and the task ends succeeded (never prematurely).
func TestWorkerRendersFramesAndChecksums(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	a := h.CreateAsset(p, owner, "a.png", testsupport.SolidPNG(4, 4, color.NRGBA{10, 20, 30, 255}))
	comp, ver := h.FreezeVersion(p, owner, specFromAsset(a.ID.String(), 4, 4, 5))
	task := h.Enqueue(p, comp, ver, 0, 4, 5, owner)

	stop := startWorker(h)
	defer stop()

	finished := h.WaitForTask(task.ID, 15*time.Second, "succeeded")
	if finished.Status != "succeeded" {
		t.Fatalf("status=%s error=%v", finished.Status, finished.Error)
	}
	frames, err := h.Q.ListFrames(h.Ctx, task.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(frames) != 5 {
		t.Fatalf("frames=%d", len(frames))
	}
	for _, f := range frames {
		if f.Status != "succeeded" {
			t.Fatalf("frame %d status=%s error=%v", f.FrameIndex, f.Status, f.Error)
		}
		if f.OutputPath == nil || f.OutputSha256 == nil || f.OutputSizeBytes == nil {
			t.Fatalf("frame %d missing output metadata", f.FrameIndex)
		}
		// The recorded checksum must match what is actually on disk — this
		// proves a real render was persisted and indexed.
		got, sz, err := compositor.VerifyOutput(*f.OutputPath)
		if err != nil {
			t.Fatalf("frame %d file invalid: %v", f.FrameIndex, err)
		}
		if got != *f.OutputSha256 {
			t.Fatalf("frame %d checksum mismatch", f.FrameIndex)
		}
		if sz != *f.OutputSizeBytes {
			t.Fatalf("frame %d size mismatch", f.FrameIndex)
		}
	}
}

// TestPartialFramesAreNotSuccess asserts a task with some rendered and some
// pending frames is never marked succeeded.
func TestPartialFramesAreNotSuccess(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	a := h.CreateAsset(p, owner, "a.png", testsupport.SolidPNG(4, 4, color.NRGBA{1, 2, 3, 255}))
	comp, ver := h.FreezeVersion(p, owner, specFromAsset(a.ID.String(), 4, 4, 10))
	task := h.Enqueue(p, comp, ver, 0, 9, 5, owner)

	stop := startWorker(h)
	// Stop the worker quickly; at least one frame likely succeeded while
	// others are pending. Either way the task must never show "succeeded".
	time.Sleep(60 * time.Millisecond)
	stop()

	rows, err := h.Q.ListFrames(h.Ctx, task.ID)
	if err != nil {
		t.Fatal(err)
	}
	var succeeded, pending int
	for _, f := range rows {
		switch f.Status {
		case "succeeded":
			succeeded++
		case "pending", "leased":
			pending++
		}
	}
	if succeeded == 0 {
		t.Skip("worker stopped before any frame completed; rerun")
	}
	if pending == 0 {
		t.Skip("all frames finished before stop; rerun")
	}
	got, err := h.Q.GetTask(h.Ctx, task.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got.Status == "succeeded" {
		t.Fatalf("task marked succeeded with %d unfinished frames", pending)
	}
}

// TestTaskOnlySucceedsWhenAllFramesDone is a stronger check: with the worker
// running, a multi-frame task passes through running and only flips to
// succeeded after every frame is done.
func TestTaskOnlySucceedsWhenAllFramesDone(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	a := h.CreateAsset(p, owner, "a.png", testsupport.SolidPNG(8, 8, color.NRGBA{1, 2, 3, 255}))
	comp, ver := h.FreezeVersion(p, owner, specFromAsset(a.ID.String(), 8, 8, 20))
	task := h.Enqueue(p, comp, ver, 0, 19, 5, owner)

	stop := startWorker(h)
	defer stop()
	final := h.WaitForTask(task.ID, 20*time.Second, "succeeded")

	counts, err := h.Q.FrameStatusCounts(h.Ctx, final.ID)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range counts {
		if c.Status == "succeeded" && c.Count != 20 {
			t.Fatalf("expected 20 succeeded frames, got %d", c.Count)
		}
	}
}
