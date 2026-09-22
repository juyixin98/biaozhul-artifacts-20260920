package testsupport

import (
	"time"

	"vfxqueue/internal/db/gen"

	"github.com/google/uuid"
)

// WaitForTask polls until the task reaches one of the wanted statuses or
// times out (failing the test).
func (h *Harness) WaitForTask(taskID uuid.UUID, timeout time.Duration, statuses ...string) gen.RenderTask {
	h.T.Helper()
	deadline := time.Now().Add(timeout)
	want := map[string]bool{}
	for _, s := range statuses {
		want[s] = true
	}
	var last gen.RenderTask
	for time.Now().Before(deadline) {
		t, err := h.Q.GetTask(h.Ctx, taskID)
		if err == nil {
			last = t
			if want[t.Status] {
				return t
			}
		}
		time.Sleep(15 * time.Millisecond)
	}
	h.T.Fatalf("task %s did not reach %v before timeout (last=%s)", taskID, statuses, last.Status)
	return last
}

// WaitForFrame polls a single frame's status.
func (h *Harness) WaitForFrame(taskID uuid.UUID, frameIndex int, timeout time.Duration, statuses ...string) gen.Frame {
	h.T.Helper()
	deadline := time.Now().Add(timeout)
	want := map[string]bool{}
	for _, s := range statuses {
		want[s] = true
	}
	var last gen.Frame
	for time.Now().Before(deadline) {
		f, err := h.Q.GetFrameForTask(h.Ctx, gen.GetFrameForTaskParams{
			TaskID: taskID, FrameIndex: int32(frameIndex),
		})
		if err == nil {
			last = f
			if want[f.Status] {
				return f
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	h.T.Fatalf("frame %d did not reach %v (last=%s err=%v)", frameIndex, statuses, last.Status, last.Error)
	return last
}
