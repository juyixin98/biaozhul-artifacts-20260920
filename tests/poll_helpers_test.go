package tests_test

import (
	"encoding/json"
	"io"
	"log"
	"net/http"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/vfxqueue/renderq/internal/testutil"
)

func discardLogger() *log.Logger {
	return log.New(io.Discard, "", 0)
}

func waitJob(t *testing.T, h *testutil.Harness, jobID uuid.UUID, want string, within time.Duration) map[string]any {
	t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		st, body := h.Do("GET", "/jobs/"+jobID.String(), testutil.AdminKey, nil)
		if st != http.StatusOK {
			t.Fatalf("get job: %d %v", st, body)
		}
		if body["status"] == want {
			return body
		}
		if s := body["status"].(string); s == "succeeded" || s == "failed" || s == "canceled" {
			if s != want {
				t.Fatalf("job reached %q, wanted %q", s, want)
			}
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("job %s did not reach %q within %s", jobID, want, within)
	return nil
}

func listFrames(t *testing.T, h *testutil.Harness, jobID uuid.UUID) []map[string]any {
	t.Helper()
	raw, err := jsonGet(h, "/jobs/"+jobID.String()+"/frames", testutil.AdminKey)
	if err != nil {
		t.Fatal(err)
	}
	var out []map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatal(err)
	}
	return out
}
