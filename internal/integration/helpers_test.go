package integration_test

import (
	"context"
	"testing"
	"time"

	"vfxqueue/internal/db/gen"
	"vfxqueue/internal/testsupport"
	"vfxqueue/internal/worker"

	"github.com/google/uuid"
)

func startWorker(h *testsupport.Harness) func() {
	ctx, cancel := context.WithCancel(h.Ctx)
	w := worker.New(h.Pool, h.Q, h.Config())
	done := make(chan struct{})
	go func() { w.Run(ctx); close(done) }()
	return func() {
		cancel()
		<-done
	}
}

func frameByIndex(h *testsupport.Harness, taskID uuid.UUID) map[int]gen.Frame {
	rows, err := h.Q.ListFrames(h.Ctx, taskID)
	if err != nil {
		h.T.Fatal(err)
	}
	out := make(map[int]gen.Frame, len(rows))
	for _, f := range rows {
		out[int(f.FrameIndex)] = f
	}
	return out
}

func countStatus(h *testsupport.Harness, taskID uuid.UUID, status string) int {
	rows, err := h.Q.ListFrames(h.Ctx, taskID)
	if err != nil {
		h.T.Fatal(err)
	}
	n := 0
	for _, f := range rows {
		if f.Status == status {
			n++
		}
	}
	return n
}

func waitSucceededCount(h *testsupport.Harness, taskID uuid.UUID, want int, timeout time.Duration) {
	h.T.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if countStatus(h, taskID, "succeeded") >= want {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	h.T.Fatalf("did not reach %d succeeded frames in %s", want, timeout)
}

var _ = testing.Short
