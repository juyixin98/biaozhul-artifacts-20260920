package tests_test

import (
	"net/http"
	"sync"
	"testing"
	"time"

	"github.com/vfxqueue/renderq/internal/testutil"
	"github.com/vfxqueue/renderq/internal/worker"
)

// TestFrameRetriesThreeTimesThenFails: a permanently failing frame is
// attempted 4 times total (1 + 3 retries), the frame is marked failed with
// a locatable error and the job is 'failed', not 'succeeded'.
func TestFrameRetriesThreeTimesThenFails(t *testing.T) {
	h := testutil.New(t)
	_, _, versionID := seedComposition(t, h, testutil.AliceKey)
	jobID := createJob(t, h, testutil.AliceKey, versionID, 5, 0, 0)

	var mu sync.Mutex
	calls := 0
	fault := func(jobID string, frameNo, attempt int) error {
		mu.Lock()
		calls++
		mu.Unlock()
		return errBoom{}
	}
	wk := worker.New(h.Pool, h.Store, 60, 1000, 5, fault, discardLogger())
	for i := 0; i < 10; i++ {
		if _, err := wk.RunOnce(h.Ctx); err != nil {
			t.Fatal(err)
		}
		_, body := h.Do("GET", "/jobs/"+jobID.String(), testutil.AdminKey, nil)
		if body["status"] == "failed" || body["status"] == "succeeded" {
			break
		}
	}

	st, body := h.Do("GET", "/jobs/"+jobID.String(), testutil.AdminKey, nil)
	if st != http.StatusOK {
		t.Fatalf("get job: %d", st)
	}
	if body["status"] != "failed" {
		t.Fatalf("status = %v, want failed", body["status"])
	}
	if calls != 4 {
		t.Fatalf("frame tried %d times, want 4", calls)
	}
	frames := listFrames(t, h, jobID)
	f := frames[0]
	if f["status"] != "failed" {
		t.Fatalf("frame status = %v", f["status"])
	}
	if f["attempts"].(float64) != 4 {
		t.Fatalf("attempts = %v, want 4", f["attempts"])
	}
	errMsg, _ := f["lastError"].(string)
	if errMsg == "" || errMsg != "boom" {
		t.Fatalf("lastError = %q, want locatable boom", errMsg)
	}
}

// TestRetrySucceedsOnSecondAttempt: a transient fault on the first attempt
// only results in a successful frame with 2 attempts.
func TestRetrySucceedsOnSecondAttempt(t *testing.T) {
	h := testutil.New(t)
	_, _, versionID := seedComposition(t, h, testutil.AliceKey)
	jobID := createJob(t, h, testutil.AliceKey, versionID, 5, 0, 0)

	var mu sync.Mutex
	seen := 0
	fault := func(jobID string, frameNo, attempt int) error {
		mu.Lock()
		defer mu.Unlock()
		seen++
		if seen == 1 {
			return errBoom{}
		}
		return nil
	}
	wk := worker.New(h.Pool, h.Store, 60, 1000, 5, fault, discardLogger())
	for i := 0; i < 10; i++ {
		if _, err := wk.RunOnce(h.Ctx); err != nil {
			t.Fatal(err)
		}
		_, body := h.Do("GET", "/jobs/"+jobID.String(), testutil.AdminKey, nil)
		if body["status"] == "succeeded" {
			break
		}
	}
	body := waitJob(t, h, jobID, "succeeded", 10*time.Second)
	if body["frameCounts"].(map[string]any)["succeeded"].(float64) != 1 {
		t.Fatalf("counts = %v", body["frameCounts"])
	}
	f := listFrames(t, h, jobID)[0]
	if f["attempts"].(float64) != 2 || f["status"] != "succeeded" {
		t.Fatalf("frame = %v", f)
	}
}

// TestPartialFailureIsNotWholeSuccess: with two frames one permanently
// failing, the job is failed and the sibling success is still retrievable.
func TestPartialFailureIsNotWholeSuccess(t *testing.T) {
	h := testutil.New(t)
	_, _, versionID := seedComposition(t, h, testutil.AliceKey)
	jobID := createJob(t, h, testutil.AliceKey, versionID, 5, 0, 1)

	var mu sync.Mutex
	perFrameCalls := map[int]int{}
	fault := func(jobID string, frameNo, attempt int) error {
		mu.Lock()
		defer mu.Unlock()
		perFrameCalls[frameNo]++
		if frameNo == 1 {
			return errBoom{}
		}
		return nil
	}
	wk := worker.New(h.Pool, h.Store, 60, 1000, 5, fault, discardLogger())
	for i := 0; i < 20; i++ {
		if _, err := wk.RunOnce(h.Ctx); err != nil {
			t.Fatal(err)
		}
		_, body := h.Do("GET", "/jobs/"+jobID.String(), testutil.AdminKey, nil)
		if body["status"] == "failed" {
			break
		}
	}
	body := waitJob(t, h, jobID, "failed", 15*time.Second)
	counts := body["frameCounts"].(map[string]any)
	if counts["succeeded"].(float64) != 1 || counts["failed"].(float64) != 1 {
		t.Fatalf("counts = %v", counts)
	}
	// The good frame's file exists; the summary endpoint is correctly
	// refused for a failed job (no partial-output-as-success).
	st, _ := h.DoRaw("GET", "/jobs/"+jobID.String()+"/summary", testutil.AdminKey, "", nil)
	if st != http.StatusConflict {
		t.Fatalf("summary on failed job: status = %d want 409", st)
	}
	status, raw := h.DoRaw("GET", "/jobs/"+jobID.String()+"/frames/0/png", testutil.AliceKey, "", nil)
	_ = raw
	if status != http.StatusOK {
		t.Fatalf("succeeded frame png: %d", status)
	}
}

type errBoom struct{}

func (errBoom) Error() string { return "boom" }
