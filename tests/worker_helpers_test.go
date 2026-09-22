package tests_test

import (
	"bytes"
	"fmt"
	"io"
	"log"
	"mime/multipart"
	"net/http"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/vfxqueue/renderq/internal/testutil"
	"github.com/vfxqueue/renderq/internal/worker"
)

// runWorker drives worker.RunOnce loops until the job reaches want or fails
// the test. A fresh worker instance (with a distinct worker id) is used per
// call so tests can simulate process restarts by calling it again.
func runWorker(t *testing.T, h *testutil.Harness, lease, renew int) *worker.Worker {
	t.Helper()
	wk := worker.New(h.Pool, h.Store, lease, renew, 5, nil,
		log.New(io.Discard, "", 0))
	return wk
}

func runOneAndWait(t *testing.T, h *testutil.Harness, jobID uuid.UUID, want string) map[string]any {
	t.Helper()
	wk := runWorker(t, h, 60, 1000)
	return driveUntil(t, h, wk, jobID, want)
}

// runWorkerWithFault drives rendering with an injected per-attempt fault.
func runWorkerWithFault(t *testing.T, h *testutil.Harness, fault worker.FaultHook) *worker.Worker {
	t.Helper()
	return worker.New(h.Pool, h.Store, 60, 1000, 5, fault,
		log.New(io.Discard, "", 0))
}

func driveUntil(t *testing.T, h *testutil.Harness, wk *worker.Worker, jobID uuid.UUID, want string) map[string]any {
	t.Helper()
	deadline := time.Now().Add(30 * time.Second)
	idle := 0
	for time.Now().Before(deadline) {
		worked, err := wk.RunOnce(h.Ctx)
		if err != nil {
			t.Fatalf("worker step: %v", err)
		}
		st, body := h.Do("GET", "/jobs/"+jobID.String(), testutil.AdminKey, nil)
		if st != http.StatusOK {
			t.Fatalf("job get: %d %v", st, body)
		}
		status := body["status"].(string)
		if status == want {
			return body
		}
		if !worked {
			idle++
			if idle > 4 && isTerminal(status) && status != want {
				t.Fatalf("job reached %q, wanted %q", status, want)
			}
			time.Sleep(20 * time.Millisecond)
		} else {
			idle = 0
		}
	}
	t.Fatalf("job %s did not reach %s", jobID, want)
	return nil
}

func isTerminal(s string) bool {
	return s == "succeeded" || s == "failed" || s == "canceled"
}

func jsonGet(h *testutil.Harness, path, key string) ([]byte, error) {
	req, _ := http.NewRequestWithContext(h.Ctx, "GET", h.BaseURL()+path, nil)
	req.Header.Set("X-API-Key", key)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 300 {
		return raw, fmt.Errorf("status %d", resp.StatusCode)
	}
	return raw, nil
}

func newMultipart(body *bytes.Buffer, fieldName, contentType string, content []byte) *multipart.Writer {
	mw := multipart.NewWriter(body)
	_ = mw.WriteField("path", fieldName)
	part, _ := mw.CreateFormFile("file", fieldName)
	_, _ = part.Write(content)
	_ = mw.Close()
	return mw
}

func newUploadRequest(h *testutil.Harness, projectID uuid.UUID, body *bytes.Buffer, contentType string) *http.Request {
	req, _ := http.NewRequestWithContext(h.Ctx, "POST",
		fmt.Sprintf("%s/projects/%s/assets", h.BaseURL(), projectID), body)
	req.Header.Set("Content-Type", contentType)
	req.Header.Set("X-API-Key", testutil.AliceKey)
	return req
}
