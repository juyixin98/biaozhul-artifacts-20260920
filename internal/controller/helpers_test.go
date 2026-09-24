package controller_test

import (
	"context"
	"fmt"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	snapshotv1alpha1 "snapshotcontroller/api/v1alpha1"
	"snapshotcontroller/internal/controller"
)

const (
	pollInterval = 20 * time.Millisecond
	pollTimeout  = 5 * time.Second
)

// waitFor polls fn until it returns nil or the timeout elapses.
func waitFor(timeout time.Duration, fn func() error) error {
	deadline := time.Now().Add(timeout)
	var lastErr error
	for time.Now().Before(deadline) {
		if lastErr = fn(); lastErr == nil {
			return nil
		}
		time.Sleep(pollInterval)
	}
	return fmt.Errorf("timed out: %w", lastErr)
}

func waitPhase(ctx context.Context, h *testHarness, name string, want snapshotv1alpha1.SnapshotPhase) (*snapshotv1alpha1.Snapshot, error) {
	var got *snapshotv1alpha1.Snapshot
	err := waitFor(pollTimeout, func() error {
		s := h.getSnapshot(ctx, name)
		if s.Status.Phase != want {
			return fmt.Errorf("phase=%s, want=%s observedGen=%d gen=%d",
				s.Status.Phase, want, s.Status.ObservedGeneration, s.Generation)
		}
		got = s
		return nil
	})
	return got, err
}

// publishWorkerResult simulates the worker pod: it creates the result ConfigMap
// the exact way worker.sh does (same labels, same data) for the given Job.
func publishWorkerResult(ctx context.Context, h *testHarness, s *snapshotv1alpha1.Snapshot, generation int64, data map[string]string) *corev1.ConfigMap {
	jobName := controller.JobName(s.Name, generation)
	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      jobName,
			Namespace: h.ns,
			Labels: map[string]string{
				"app.kubernetes.io/part-of":       "recoverable-snapshot-controller",
				"snapshot.example.com/managed-by": "snapshot-controller",
				"snapshot.example.com/snapshot":   s.Name,
				"snapshot.example.com/generation": fmt.Sprintf("%d", generation),
			},
		},
		Data: data,
	}
	if err := h.k8s.Create(ctx, cm); err != nil {
		h.t.Fatalf("create result configmap: %v", err)
	}
	return cm
}

func successResultData(digest, manifest string, gen int64) map[string]string {
	return map[string]string{
		"algorithm":       "SHA-256",
		"digest":          digest,
		"fileCount":       "2",
		"totalBytes":      "11",
		"sourceConfigMap": "src",
		"subPath":         "",
		"generation":      fmt.Sprintf("%d", gen),
		"manifest":        manifest,
	}
}

// markJobSucceeded sets Job status as a completed Job controller would.
func markJobSucceeded(ctx context.Context, h *testHarness, jobName string) {
	var j batchv1.Job
	if err := h.k8s.Get(ctx, client.ObjectKey{Namespace: h.ns, Name: jobName}, &j); err != nil {
		h.t.Fatalf("get job for status update: %v", err)
	}
	j.Status.Succeeded = 1
	j.Status.Conditions = []batchv1.JobCondition{{
		Type:   batchv1.JobComplete,
		Status: corev1.ConditionTrue,
		Reason: "",
	}}
	if err := h.k8s.Status().Update(ctx, &j); err != nil {
		h.t.Fatalf("update job status: %v", err)
	}
}

// markJobFailed sets Job status as exhausted/failed.
func markJobFailed(ctx context.Context, h *testHarness, jobName string) {
	var j batchv1.Job
	if err := h.k8s.Get(ctx, client.ObjectKey{Namespace: h.ns, Name: jobName}, &j); err != nil {
		h.t.Fatalf("get job for status update: %v", err)
	}
	j.Status.Failed = 2
	j.Status.Conditions = []batchv1.JobCondition{{
		Type:    batchv1.JobFailed,
		Status:  corev1.ConditionTrue,
		Reason:  "BackoffLimitExceeded",
		Message: "Job has reached the specified backoff limit",
	}}
	if err := h.k8s.Status().Update(ctx, &j); err != nil {
		h.t.Fatalf("update job status: %v", err)
	}
}

func createSnapshotAndSource(ctx context.Context, h *testHarness, name string) *snapshotv1alpha1.Snapshot {
	src := sourceConfigMap("src", h.ns, map[string]string{"a.txt": "hello-a", "b.txt": "hello-bbb"})
	if err := h.k8s.Create(ctx, src); err != nil {
		h.t.Fatalf("create source: %v", err)
	}
	s := newSnapshot(name, h.ns, "src")
	if err := h.k8s.Create(ctx, s); err != nil {
		h.t.Fatalf("create snapshot: %v", err)
	}
	return s
}
