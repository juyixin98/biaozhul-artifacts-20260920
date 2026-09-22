package integration_test

import (
	"image/color"
	"os"
	"testing"
	"time"

	"vfxqueue/internal/db/gen"
	"vfxqueue/internal/storage"
	"vfxqueue/internal/testsupport"
)

// TestFrameRetryThreeTimesThenTaskFails: a version frozen to a blob that
// disappears before rendering causes every attempt to fail. The frame retries
// exactly 3 times, ends 'failed' with a locatable error, siblings are
// cancelled, and the task ends 'failed'.
func TestFrameRetryThreeTimesThenTaskFails(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	a := h.CreateAsset(p, owner, "a.png", testsupport.SolidPNG(4, 4, color.NRGBA{1, 2, 3, 255}))
	comp, ver := h.FreezeVersion(p, owner, specFromAsset(a.ID.String(), 4, 4, 3))
	task := h.Enqueue(p, comp, ver, 0, 2, 5, owner)

	if err := os.Remove(a.StoragePath); err != nil {
		t.Fatal(err)
	}

	stop := startWorker(h)
	defer stop()

	final := h.WaitForTask(task.ID, 20*time.Second, "failed")
	if final.Error == nil || *final.Error == "" {
		t.Fatal("failed task must carry an error")
	}
	frames, err := h.Q.ListFrames(h.Ctx, task.ID)
	if err != nil {
		t.Fatal(err)
	}
	var failed, cancelled, succeeded int
	var badFrame *gen.Frame
	for i := range frames {
		f := &frames[i]
		switch f.Status {
		case "failed":
			failed++
			badFrame = f
		case "cancelled":
			cancelled++
		case "succeeded":
			succeeded++
		}
	}
	if failed != 1 {
		t.Fatalf("want exactly 1 failed frame, got %d", failed)
	}
	if cancelled != 2 {
		t.Fatalf("want 2 cancelled sibling frames, got %d", cancelled)
	}
	if succeeded != 0 {
		t.Fatalf("want 0 succeeded, got %d", succeeded)
	}
	if badFrame.Attempts != 3 {
		t.Fatalf("frame attempts=%d, want exactly 3", badFrame.Attempts)
	}
	if badFrame.Error == nil || *badFrame.Error == "" {
		t.Fatal("failed frame must carry a locatable error")
	}
}

// TestRetryRecoversOnSecondAttempt: first attempt fails (missing blob), second
// succeeds once the blob is restored.
func TestRetryRecoversOnSecondAttempt(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	data := testsupport.SolidPNG(4, 4, color.NRGBA{5, 6, 7, 255})
	a := h.CreateAsset(p, owner, "a.png", data)
	comp, ver := h.FreezeVersion(p, owner, specFromAsset(a.ID.String(), 4, 4, 1))
	task := h.Enqueue(p, comp, ver, 0, 0, 5, owner)

	if err := os.Remove(a.StoragePath); err != nil {
		t.Fatal(err)
	}
	stop := startWorker(h)

	// Wait until the first attempt has been spent (attempts counter advances),
	// then restore the blob so a later attempt succeeds.
	deadline := time.Now().Add(5 * time.Second)
	var f0 gen.Frame
	for time.Now().Before(deadline) {
		f0, _ = h.Q.GetFrameForTask(h.Ctx, gen.GetFrameForTaskParams{
			TaskID: task.ID, FrameIndex: 0,
		})
		if f0.Attempts >= 1 {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	if f0.Attempts < 1 {
		t.Fatalf("expected at least 1 failed attempt, got %d", f0.Attempts)
	}
	if err := storage.AtomicWriteFile(a.StoragePath, data); err != nil {
		t.Fatal(err)
	}
	defer stop()

	h.WaitForTask(task.ID, 15*time.Second, "succeeded")
	ff, err := h.Q.GetFrameForTask(h.Ctx, gen.GetFrameForTaskParams{
		TaskID: task.ID, FrameIndex: 0,
	})
	if err != nil {
		t.Fatal(err)
	}
	if ff.Status != "succeeded" {
		t.Fatalf("frame status=%s", ff.Status)
	}
	if ff.Attempts < 2 {
		t.Fatalf("expected >=2 attempts after recovery, got %d", ff.Attempts)
	}
}

// TestRestartResumesCompletedFrames: render some frames, kill the worker,
// restart it; already-succeeded frames keep their checksums and the task
// completes from where it left off.
func TestRestartResumesCompletedFrames(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	a := h.CreateAsset(p, owner, "a.png", testsupport.SolidPNG(4, 4, color.NRGBA{9, 9, 9, 255}))
	comp, ver := h.FreezeVersion(p, owner, specFromAsset(a.ID.String(), 4, 4, 30))
	task := h.Enqueue(p, comp, ver, 0, 29, 5, owner)

	stop1 := startWorker(h)
	waitSucceededCount(h, task.ID, 5, 5*time.Second)
	stop1()

	before := frameByIndex(h, task.ID)
	stop2 := startWorker(h)
	defer stop2()
	h.WaitForTask(task.ID, 20*time.Second, "succeeded")

	frames, _ := h.Q.ListFrames(h.Ctx, task.ID)
	if len(frames) != 30 {
		t.Fatalf("want 30 frames, got %d", len(frames))
	}
	after := frameByIndex(h, task.ID)
	for idx, f := range after {
		if f.Status != "succeeded" {
			t.Fatalf("frame %d status=%s", idx, f.Status)
		}
		if b, ok := before[idx]; ok && b.Status == "succeeded" {
			if b.OutputSha256 == nil || f.OutputSha256 == nil ||
				*b.OutputSha256 != *f.OutputSha256 {
				t.Fatalf("frame %d checksum changed across restart", idx)
			}
		}
	}
}
