package integration_test

import (
	"image/color"
	"os"
	"path/filepath"
	"testing"
	"time"

	"vfxqueue/internal/reconcile"
	"vfxqueue/internal/testsupport"
)

// TestReconcile_ResumesFromCompletedFrames simulates a crash mid-task: some
// frames succeeded on disk+db, some are still leased by the dead worker. After
// reconciliation, stale leases reset, succeeded frames keep their output, and
// a restarted worker finishes the rest without redoing done frames.
func TestReconcile_ResumesFromCompletedFrames(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	a := h.CreateAsset(p, owner, "a.png", testsupport.SolidPNG(4, 4, color.NRGBA{2, 3, 4, 255}))
	comp, ver := h.FreezeVersion(p, owner, specFromAsset(a.ID.String(), 4, 4, 8))
	task := h.Enqueue(p, comp, ver, 0, 7, 5, owner)

	stop := startWorker(h)
	waitSucceededCount(h, task.ID, 3, 5*time.Second)
	stop() // "crash": any leases now belong to a dead process

	done := frameByIndex(h, task.ID)
	rec := reconcile.New(h.Pool, h.Q, h.OutputsDir())
	if err := rec.Run(h.Ctx); err != nil {
		t.Fatal(err)
	}

	// Frames that were open-leased at crash are pending again; frames that
	// were succeeded with a good file stay succeeded.
	after := frameByIndex(h, task.ID)
	for idx, f := range after {
		before := done[idx]
		if before.Status == "succeeded" {
			if f.Status != "succeeded" {
				t.Fatalf("frame %d reverted from succeeded after reconcile", idx)
			}
			continue
		}
		if f.Status != "pending" {
			t.Fatalf("frame %d status=%s, want pending", idx, f.Status)
		}
	}

	stop2 := startWorker(h)
	defer stop2()
	h.WaitForTask(task.ID, 20*time.Second, "succeeded")
}

// TestReconcile_MissingOutputFileRequeues: a frame row says succeeded but the
// file was lost (crash between file write and durability) -> reconcile resets
// it and the worker re-renders it; the task is not allowed to stay succeeded
// with missing output.
func TestReconcile_MissingOutputFileRequeues(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	a := h.CreateAsset(p, owner, "a.png", testsupport.SolidPNG(4, 4, color.NRGBA{5, 5, 5, 255}))
	comp, ver := h.FreezeVersion(p, owner, specFromAsset(a.ID.String(), 4, 4, 2))
	task := h.Enqueue(p, comp, ver, 0, 1, 5, owner)

	stop := startWorker(h)
	h.WaitForTask(task.ID, 10*time.Second, "succeeded")
	stop()

	frames, _ := h.Q.ListFrames(h.Ctx, task.ID)
	if len(frames) != 2 {
		t.Fatalf("frames=%d", len(frames))
	}
	victim := frames[0]
	if victim.OutputPath == nil {
		t.Fatal("expected output path")
	}
	if err := os.Remove(*victim.OutputPath); err != nil {
		t.Fatal(err)
	}

	rec := reconcile.New(h.Pool, h.Q, h.OutputsDir())
	if err := rec.Run(h.Ctx); err != nil {
		t.Fatal(err)
	}

	reset, err := h.Q.GetFrame(h.Ctx, victim.ID)
	if err != nil {
		t.Fatal(err)
	}
	if reset.Status != "pending" {
		t.Fatalf("missing-file frame status=%s, want pending", reset.Status)
	}
	rt, err := h.Q.GetTask(h.Ctx, task.ID)
	if err != nil {
		t.Fatal(err)
	}
	if rt.Status == "succeeded" {
		t.Fatal("task must not remain succeeded when a frame's output is lost")
	}

	stop2 := startWorker(h)
	defer stop2()
	final := h.WaitForTask(task.ID, 15*time.Second, "succeeded")
	if final.Status != "succeeded" {
		t.Fatalf("status=%s", final.Status)
	}
	rebuilt, err := h.Q.GetFrame(h.Ctx, victim.ID)
	if err != nil {
		t.Fatal(err)
	}
	if rebuilt.OutputPath == nil {
		t.Fatal("re-rendered frame has no output path")
	}
	if _, err := os.Stat(*rebuilt.OutputPath); err != nil {
		t.Fatalf("rebuilt file missing: %v", err)
	}
}

// TestReconcile_CleansTempAndOrphans: interrupted .tmp files and output files
// with no succeeded frame row are garbage collected on restart.
func TestReconcile_CleansTempAndOrphans(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	a := h.CreateAsset(p, owner, "a.png", testsupport.SolidPNG(4, 4, color.NRGBA{8, 8, 8, 255}))
	comp, ver := h.FreezeVersion(p, owner, specFromAsset(a.ID.String(), 4, 4, 1))
	task := h.Enqueue(p, comp, ver, 0, 0, 5, owner)

	// Plant garbage inside the task output directory.
	dir := task.OutputDir
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	tmp := filepath.Join(dir, ".tmp-abandoned")
	orphan := filepath.Join(dir, "frame_000099.png")
	if err := os.WriteFile(tmp, []byte("partial"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(orphan, []byte("not a real frame"), 0o644); err != nil {
		t.Fatal(err)
	}

	rec := reconcile.New(h.Pool, h.Q, h.OutputsDir())
	if err := rec.Run(h.Ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(tmp); !os.IsNotExist(err) {
		t.Fatal("temp file not cleaned")
	}
	if _, err := os.Stat(orphan); !os.IsNotExist(err) {
		t.Fatal("orphan output not cleaned")
	}

	// The real frame renders successfully afterwards.
	stop := startWorker(h)
	defer stop()
	h.WaitForTask(task.ID, 15*time.Second, "succeeded")
}
