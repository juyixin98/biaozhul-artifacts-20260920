package integration_test

import (
	"image/color"
	"testing"
	"time"

	"vfxqueue/internal/testsupport"

	"github.com/google/uuid"
)

// TestCancelQueuedTask: cancel before any worker runs -> task and every frame
// become cancelled; starting a worker afterward renders nothing.
func TestCancelQueuedTask(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	a := h.CreateAsset(p, owner, "a.png", testsupport.SolidPNG(4, 4, color.NRGBA{3, 2, 1, 255}))
	comp, ver := h.FreezeVersion(p, owner, specFromAsset(a.ID.String(), 4, 4, 5))
	task := h.Enqueue(p, comp, ver, 0, 4, 5, owner)

	if _, err := h.Q.CancelTask(h.Ctx, task.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := h.Q.CancelRemainingFrames(h.Ctx, task.ID); err != nil {
		t.Fatal(err)
	}

	stop := startWorker(h)
	defer stop()
	time.Sleep(400 * time.Millisecond)

	got, err := h.Q.GetTask(h.Ctx, task.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != "cancelled" {
		t.Fatalf("status=%s", got.Status)
	}
	frames, _ := h.Q.ListFrames(h.Ctx, task.ID)
	for _, f := range frames {
		if f.Status != "cancelled" {
			t.Fatalf("frame %d status=%s, want cancelled", f.FrameIndex, f.Status)
		}
		if f.OutputPath != nil {
			t.Fatalf("cancelled frame %d produced output", f.FrameIndex)
		}
	}
}

// TestCancelRace_OnlyOneTerminal: cancel and complete concurrently for many
// one-frame tasks. Each task must finish in exactly one terminal state, never
// change afterwards, and a cancelled task must never carry a succeeded frame.
func TestCancelRace_OnlyOneTerminal(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	a := h.CreateAsset(p, owner, "a.png", testsupport.SolidPNG(4, 4, color.NRGBA{9, 9, 9, 255}))

	const n = 40
	var taskIDs []uuid.UUID
	for i := 0; i < n; i++ {
		comp, ver := h.FreezeVersion(p, owner, specFromAsset(a.ID.String(), 4, 4, 1))
		task := h.Enqueue(p, comp, ver, 0, 0, 5, owner)
		taskIDs = append(taskIDs, task.ID)
	}

	stop := startWorker(h)

	go func() {
		for _, id := range taskIDs {
			tx, err := h.Pool.Begin(h.Ctx)
			if err != nil {
				continue
			}
			qtx := h.Q.WithTx(tx)
			if _, err := qtx.LockTask(h.Ctx, id); err == nil {
				if _, err := qtx.CancelTask(h.Ctx, id); err == nil {
					_, _ = qtx.CancelRemainingFrames(h.Ctx, id)
				}
			}
			_ = tx.Commit(h.Ctx)
		}
	}()

	deadline := time.Now().Add(20 * time.Second)
	final := map[uuid.UUID]string{}
	for time.Now().Before(deadline) && len(final) < n {
		for _, id := range taskIDs {
			if _, seen := final[id]; seen {
				continue
			}
			tg, err := h.Q.GetTask(h.Ctx, id)
			if err == nil && (tg.Status == "succeeded" || tg.Status == "cancelled" || tg.Status == "failed") {
				final[id] = tg.Status
			}
		}
		time.Sleep(5 * time.Millisecond)
	}
	stop()

	if len(final) != n {
		t.Fatalf("only %d/%d tasks reached a terminal state", len(final), n)
	}

	// Terminal states must be stable.
	time.Sleep(300 * time.Millisecond)
	var nSucceeded, nCancelled int
	for _, id := range taskIDs {
		tg, err := h.Q.GetTask(h.Ctx, id)
		if err != nil {
			t.Fatal(err)
		}
		if final[id] != tg.Status {
			t.Fatalf("task %s flipped %s -> %s", id, final[id], tg.Status)
		}
		switch tg.Status {
		case "cancelled":
			nCancelled++
			frames, _ := h.Q.ListFrames(h.Ctx, id)
			for _, f := range frames {
				if f.Status == "succeeded" {
					t.Fatalf("cancelled task %s has succeeded frame %d", id, f.FrameIndex)
				}
			}
		case "succeeded":
			nSucceeded++
		}
	}
	if nSucceeded == 0 || nCancelled == 0 {
		t.Fatalf("race produced no contention: succeeded=%d cancelled=%d", nSucceeded, nCancelled)
	}
	t.Logf("race outcome: %d succeeded, %d cancelled", nSucceeded, nCancelled)
}
