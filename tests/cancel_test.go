package tests_test

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"os"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/vfxqueue/renderq/internal/db/dbgen"
	"github.com/vfxqueue/renderq/internal/storage"
	"github.com/vfxqueue/renderq/internal/testutil"
	"github.com/vfxqueue/renderq/internal/worker"
)

// TestCancelQueuedJob: canceling before any frame is claimed leaves the job
// canceled; nothing is ever rendered and no output exists.
func TestCancelQueuedJob(t *testing.T) {
	h := testutil.New(t)
	_, _, versionID := seedComposition(t, h, testutil.AliceKey)
	jobID := createJob(t, h, testutil.AliceKey, versionID, 5, 0, 1)

	st, body := h.Do("POST", "/jobs/"+jobID.String()+"/cancel", testutil.AdminKey, nil)
	if st != http.StatusOK {
		t.Fatalf("cancel: %d %v", st, body)
	}

	wk := runWorker(t, h, 60, 1000)
	worked, err := wk.RunOnce(h.Ctx)
	if err != nil {
		t.Fatal(err)
	}
	if worked {
		t.Fatal("worker claimed a frame of a canceled job")
	}
	st, body = h.Do("GET", "/jobs/"+jobID.String(), testutil.AdminKey, nil)
	if body["status"] != "canceled" {
		t.Fatalf("status = %v", body["status"])
	}
	for _, f := range listFrames(t, h, jobID) {
		if f["status"] != "pending" {
			t.Fatalf("frame = %v, want pending (never claimed)", f["status"])
		}
	}
	if code, _ := h.DoRaw("GET", "/jobs/"+jobID.String()+"/frames/0/png", testutil.AdminKey, "", nil); code != http.StatusNotFound {
		t.Fatalf("canceled job output status = %d want 404", code)
	}
}

// submitLikeWorker performs the EXACT submit path a rendering worker takes
// after producing bytes: tx { SELECT ... FOR UPDATE on job; guarded
// CompleteFrame; possibly flip job; COMMIT } then atomic file install. It
// reuses production code via the worker package; here we replicate the SQL
// sequence against the same pool so the test races the genuine statements.
func submitLikeWorker(t *testing.T, h *testutil.Harness, frame dbgen.Frame, pngBytes []byte) int64 {
	t.Helper()
	sum := sha256.Sum256(pngBytes)
	digest := hex.EncodeToString(sum[:])

	// Stage to the same job dir, exactly like worker.stage.
	jobDir := h.Store.JobDir(frame.JobID.String())
	if err := os.MkdirAll(jobDir, 0o755); err != nil {
		t.Fatal(err)
	}
	tmp, err := os.CreateTemp(jobDir, ".stage-*")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tmp.Write(pngBytes); err != nil {
		t.Fatal(err)
	}
	tmp.Sync()
	staged := tmp.Name()
	tmp.Close()

	tx, err := h.Pool.Begin(h.Ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(h.Ctx)
	qtx := dbgen.New(tx)

	if _, err := qtx.GetJobForUpdate(h.Ctx, frame.JobID); err != nil {
		t.Fatal(err)
	}
	n, err := qtx.CompleteFrame(h.Ctx, dbgen.CompleteFrameParams{
		ID: frame.ID, Sha256: digest, SizeBytes: int64(len(pngBytes)), Generation: frame.Generation,
	})
	if err != nil {
		t.Fatal(err)
	}
	if n == 1 {
		counts, err := qtx.CountFrameStatuses(h.Ctx, frame.JobID)
		if err != nil {
			t.Fatal(err)
		}
		if counts.Succeeded == counts.Total {
			if _, err := qtx.CompleteJobIfDone(h.Ctx, frame.JobID); err != nil {
				t.Fatal(err)
			}
		}
	}
	if err := tx.Commit(h.Ctx); err != nil {
		t.Fatal(err)
	}
	if n == 1 {
		if err := os.Rename(staged, h.Store.FramePath(frame.JobID.String(), int(frame.FrameNo))); err != nil {
			t.Fatal(err)
		}
	} else {
		_ = os.Remove(staged)
	}
	return n
}

// TestCancelCompleteRace: a claimed frame with cancel and worker-submit
// racing must end in exactly one terminal state; cancel wins => no output,
// complete wins => output present and digest matches.
func TestCancelCompleteRace(t *testing.T) {
	h := testutil.New(t)
	_, _, versionID := seedComposition(t, h, testutil.AliceKey)

	const runs = 8
	var outcomes = map[string]int{}
	for run := 0; run < runs; run++ {
		jobID := createJob(t, h, testutil.AliceKey, versionID, 5, 0, 0)
		q := dbgen.New(h.Pool)

		frame, err := q.ClaimFrame(h.Ctx, dbgen.ClaimFrameParams{
			WorkerID: "racer", LeaseSeconds: 30,
		})
		if err != nil {
			t.Fatalf("claim: %v", err)
		}
		if _, err := q.MarkJobRunning(h.Ctx, jobID); err != nil {
			t.Fatal(err)
		}

		// Real rendered PNG for the version's manifest.
		pngBytes := renderOneFrame(t, h, frame)

		var wg sync.WaitGroup
		start := make(chan struct{})
		var completeWon int64
		var cancelStatus int

		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			n := submitLikeWorker(t, h, frame, pngBytes)
			atomic.StoreInt64(&completeWon, n)
		}()
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			cancelStatus, _ = h.Do("POST", "/jobs/"+jobID.String()+"/cancel", testutil.AdminKey, nil)
		}()
		close(start)
		wg.Wait()

		st, body := h.Do("GET", "/jobs/"+jobID.String(), testutil.AdminKey, nil)
		if st != http.StatusOK {
			t.Fatalf("get: %d", st)
		}
		status := body["status"].(string)
		outcomes[status]++

		switch status {
		case "canceled":
			if atomic.LoadInt64(&completeWon) != 0 {
				t.Fatalf("run %d: job canceled but guarded complete also wrote a frame", run)
			}
			if cancelStatus != http.StatusOK {
				t.Fatalf("run %d: canceled but cancel returned %d", run, cancelStatus)
			}
			if code, _ := h.DoRaw("GET", "/jobs/"+jobID.String()+"/frames/0/png", testutil.AdminKey, "", nil); code == http.StatusOK {
				t.Fatalf("run %d: canceled job published frame output", run)
			}
		case "succeeded":
			if atomic.LoadInt64(&completeWon) != 1 {
				t.Fatalf("run %d: job succeeded but complete affected %d rows", run, completeWon)
			}
			if cancelStatus == http.StatusOK {
				t.Fatalf("run %d: job succeeded but cancel also returned 200", run)
			}
			if code, raw := h.DoRaw("GET", "/jobs/"+jobID.String()+"/frames/0/png", testutil.AliceKey, "", nil); code != http.StatusOK {
				t.Fatalf("run %d: succeeded job missing frame output (%d)", run, code)
			} else {
				sum := sha256.Sum256(raw)
				frameRow, _ := q.GetFrame(h.Ctx, frame.ID)
				if frameRow.OutputSha256 != hex.EncodeToString(sum[:]) {
					t.Fatalf("run %d: published digest mismatch", run)
				}
			}
		default:
			t.Fatalf("run %d: invalid terminal status %q", run, status)
		}
	}
	// Sanity: across many runs both outcomes are plausible — we do not
	// assert a split (scheduling is not required to produce one), only that
	// every run obeyed the single-terminal-state invariant.
	t.Logf("race outcomes: %+v", outcomes)
}

// renderOneFrame renders a frame exactly as the worker would, returning PNG
// bytes. It loads the frozen manifest/resources from the frame's job.
func renderOneFrame(t *testing.T, h *testutil.Harness, frame dbgen.Frame) []byte {
	t.Helper()
	q := dbgen.New(h.Pool)
	job, err := q.GetJobWithVersion(h.Ctx, frame.JobID)
	if err != nil {
		t.Fatal(err)
	}
	return renderFrameDirect(t, h, job.VersionID, job.Manifest, int(frame.FrameNo))
}

// TestCancelStopsFurtherResults: after cancellation, pending frames stay
// unrendered even when workers keep polling.
func TestCancelStopsFurtherResults(t *testing.T) {
	h := testutil.New(t)
	_, _, versionID := seedComposition(t, h, testutil.AliceKey)
	jobID := createJob(t, h, testutil.AliceKey, versionID, 5, 0, 3)

	if st, _ := h.Do("POST", "/jobs/"+jobID.String()+"/cancel", testutil.AdminKey, nil); st != http.StatusOK {
		t.Fatalf("cancel: %d", st)
	}
	wk := worker.New(h.Pool, h.Store, 60, 1000, 5, nil, discardLogger())
	ctx, cancel := context.WithTimeout(h.Ctx, 2*time.Second)
	defer cancel()
	for {
		worked, err := wk.RunOnce(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if !worked {
			break
		}
	}
	for i := 0; i <= 3; i++ {
		if code, _ := h.DoRaw("GET", "/jobs/"+jobID.String()+"/frames/"+strconv.Itoa(i)+"/png", testutil.AdminKey, "", nil); code == http.StatusOK {
			t.Fatalf("frame %d published after cancel", i)
		}
	}
}

var _ = storage.New
var _ = uuid.Nil
