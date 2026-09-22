package tests_test

import (
	"context"
	"net/http"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/vfxqueue/renderq/internal/db/dbgen"
	"github.com/vfxqueue/renderq/internal/testutil"
	"github.com/vfxqueue/renderq/internal/worker"
)

// createJobFree enqueues a job on a frozen version.
func createJob(t *testing.T, h *testutil.Harness, key string, versionID uuid.UUID, priority, start, end int) uuid.UUID {
	t.Helper()
	st, body := h.Do("POST", "/versions/"+versionID.String()+"/jobs", key,
		map[string]any{"priority": priority, "frameStart": start, "frameEnd": end})
	if st != http.StatusCreated {
		t.Fatalf("create job: %d %v", st, body)
	}
	return h.AsID(body, "id")
}

// TestPriorityThenFIFO: priority 1 goes first; equal priority follows
// enqueue order.
func TestPriorityThenFIFO(t *testing.T) {
	h := testutil.New(t)
	_, _, versionID := seedComposition(t, h, testutil.AliceKey)

	jobA := createJob(t, h, testutil.AliceKey, versionID, 5, 0, 0) // priority 5, first in
	time.Sleep(5 * time.Millisecond)
	jobB := createJob(t, h, testutil.AliceKey, versionID, 1, 0, 0) // priority 1
	time.Sleep(5 * time.Millisecond)
	jobC := createJob(t, h, testutil.AliceKey, versionID, 5, 0, 0) // priority 5, later

	wk := runWorker(t, h, 60, 1000)
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := wk.RunOnce(h.Ctx); err != nil {
			t.Fatal(err)
		}
		if len(querySucceededOrder(t, h)) == 3 {
			break
		}
	}
	order := querySucceededOrder(t, h)

	want := []uuid.UUID{jobB, jobA, jobC}
	if len(order) != 3 {
		t.Fatalf("only %d frames completed: %v", len(order), order)
	}
	for i := range want {
		if order[i] != want[i] {
			t.Fatalf("dispatch order = %v, want %v", order, want)
		}
	}
}

func querySucceededOrder(t *testing.T, h *testutil.Harness) []uuid.UUID {
	t.Helper()
	rows, err := h.Pool.Query(h.Ctx,
		`SELECT job_id FROM frames WHERE status='succeeded' ORDER BY updated_at, frame_no`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var out []uuid.UUID
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			t.Fatal(err)
		}
		out = append(out, id)
	}
	return out
}

// TestTwoWorkersClaimExactlyOnce: two workers on a four-frame job complete
// every frame with a single attempt each (FOR UPDATE SKIP LOCKED prevents
// duplicate claims).
func TestTwoWorkersClaimExactlyOnce(t *testing.T) {
	h := testutil.New(t)
	_, _, versionID := seedComposition(t, h, testutil.AliceKey)
	jobID := createJob(t, h, testutil.AliceKey, versionID, 5, 0, 3)

	wk := worker.New(h.Pool, h.Store, 60, 1000, 5, nil, discardLogger())
	ctx, cancel := context.WithCancel(h.Ctx)
	done := make(chan struct{})
	go func() { wk.Run(ctx, 2); close(done) }()

	body := waitJob(t, h, jobID, "succeeded", 20*time.Second)
	cancel()
	<-done

	counts := body["frameCounts"].(map[string]any)
	if counts["succeeded"].(float64) != 4 {
		t.Fatalf("counts = %v", counts)
	}
	frames := listFrames(t, h, jobID)
	for _, f := range frames {
		if f["attempts"].(float64) != 1 {
			t.Fatalf("frame %v processed %v times, want 1", f["frameNo"], f["attempts"])
		}
	}
}

// TestLeaseExpiryAndReclaim: a dead worker's expired lease is reclaimed by
// a live worker; the reclaim bumps both attempts and generation.
func TestLeaseExpiryAndReclaim(t *testing.T) {
	h := testutil.New(t)
	_, _, versionID := seedComposition(t, h, testutil.AliceKey)
	jobID := createJob(t, h, testutil.AliceKey, versionID, 5, 0, 0)

	q := dbgen.New(h.Pool)
	dead, err := q.ClaimFrame(h.Ctx, dbgen.ClaimFrameParams{
		WorkerID: "dead-w-1", LeaseSeconds: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if dead.Generation != 1 || dead.Attempts != 1 {
		t.Fatalf("initial claim gen=%d attempts=%d", dead.Generation, dead.Attempts)
	}

	// Before expiry the live worker must NOT steal the frame.
	live := runWorker(t, h, 60, 1000)
	worked, err := live.RunOnce(h.Ctx)
	if err != nil {
		t.Fatal(err)
	}
	if worked {
		t.Fatal("live worker stole a non-expired lease")
	}

	time.Sleep(1100 * time.Millisecond)

	worked, err = live.RunOnce(h.Ctx)
	if err != nil || !worked {
		t.Fatalf("reclaim worked=%v err=%v", worked, err)
	}
	body := waitJob(t, h, jobID, "succeeded", 10*time.Second)
	if body["status"] != "succeeded" {
		t.Fatalf("status = %v", body["status"])
	}
	frames := listFrames(t, h, jobID)
	f := frames[0]
	if f["generation"].(float64) != 2 {
		t.Fatalf("generation = %v, want 2", f["generation"])
	}
	if f["attempts"].(float64) != 2 {
		t.Fatalf("attempts = %v, want 2", f["attempts"])
	}
}

// TestLateSubmitRejected: once a frame is reclaimed and completed by a new
// generation, the old worker's late completion must affect zero rows and
// must not overwrite the published digest.
func TestLateSubmitRejected(t *testing.T) {
	h := testutil.New(t)
	_, _, versionID := seedComposition(t, h, testutil.AliceKey)
	jobID := createJob(t, h, testutil.AliceKey, versionID, 5, 0, 0)

	q := dbgen.New(h.Pool)
	dead, err := q.ClaimFrame(h.Ctx, dbgen.ClaimFrameParams{
		WorkerID: "dead", LeaseSeconds: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(1100 * time.Millisecond)
	live := runWorker(t, h, 60, 1000)
	if _, err := live.RunOnce(h.Ctx); err != nil {
		t.Fatal(err)
	}
	waitJob(t, h, jobID, "succeeded", 10*time.Second)

	winning, err := q.GetFrame(h.Ctx, dead.ID)
	if err != nil {
		t.Fatal(err)
	}
	if winning.Generation != 2 || winning.Status != "succeeded" {
		t.Fatalf("winning frame gen=%d status=%s", winning.Generation, winning.Status)
	}
	winDigest := winning.OutputSha256

	// The dead worker finally reports its result with stale generation 1.
	n, err := q.CompleteFrame(h.Ctx, dbgen.CompleteFrameParams{
		ID: dead.ID, Sha256: "deadbeef" + winDigest[8:], SizeBytes: 999, Generation: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("late submit affected %d rows, want 0", n)
	}
	after, _ := q.GetFrame(h.Ctx, dead.ID)
	if after.OutputSha256 != winDigest || after.OutputSize != winning.OutputSize {
		t.Fatal("late submit overwrote the winning result")
	}
	if after.Generation != 2 {
		t.Fatal("late submit changed generation")
	}
}
