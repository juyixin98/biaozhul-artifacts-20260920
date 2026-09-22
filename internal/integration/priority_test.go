package integration_test

import (
	"image/color"
	"testing"

	"vfxqueue/internal/testsupport"

	"github.com/google/uuid"
)

// TestPriorityAndFIFO: a higher-priority task's frames are all claimed before
// any lower-priority task's; within equal priority, earlier-enqueued tasks go
// first.
func TestPriorityAndFIFO(t *testing.T) {
	h := testsupport.New(t)
	owner := h.CreateUser("member")
	p := h.CreateProject(owner)
	a := h.CreateAsset(p, owner, "a.png", testsupport.SolidPNG(4, 4, color.NRGBA{1, 1, 1, 255}))

	// Enqueue low first, then medium, then high. Each has 3 frames.
	mk := func(priority int) uuid.UUID {
		comp, ver := h.FreezeVersion(p, owner, specFromAsset(a.ID.String(), 4, 4, 3))
		return h.Enqueue(p, comp, ver, 0, 2, priority, owner).ID
	}
	low := mk(1)
	mid := mk(5)
	high := mk(10)
	// And one more FIFO low, to confirm FIFO inside a priority.
	low2 := mk(1)

	// Claim 12 frames serially without rendering completion (so task stays
	// running and no frames complete to reorder things). Record each claimed
	// frame's task order.
	var order []uuid.UUID
	for i := 0; i < 12; i++ {
		tok := uuid.New()
		row, err := h.ClaimOnce(tok, "probe")
		if err != nil {
			t.Fatalf("claim %d: %v", i, err)
		}
		order = append(order, row.TaskID)
	}

	first3 := order[0:3]
	next3 := order[3:6]
	last6 := order[6:12]
	all := func(ids []uuid.UUID, want uuid.UUID) bool {
		for _, id := range ids {
			if id != want {
				return false
			}
		}
		return true
	}
	if !all(first3, high) {
		t.Fatalf("high priority not first: %v", first3)
	}
	if !all(next3, mid) {
		t.Fatalf("medium priority not second: %v", next3)
	}
	if !all(last6[:3], low) || !all(last6[3:], low2) {
		t.Fatalf("low tasks not FIFO: %v", last6)
	}
}
