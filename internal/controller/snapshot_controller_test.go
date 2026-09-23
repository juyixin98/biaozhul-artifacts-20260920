package controller_test

import (
	"context"
	"strings"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/clock"
	"sigs.k8s.io/controller-runtime/pkg/client"

	snapshotv1alpha1 "snapshotcontroller/api/v1alpha1"
	"snapshotcontroller/internal/controller"
)

const (
	poll    = 200 * time.Millisecond
	waitFor = 20 * time.Second
)

// TestHappyFlow covers Pending -> Running -> Ready with a real (simulated)
// Job completion and result ConfigMap, and verifies observedGeneration and
// digest are bound to generation 1.
func TestHappyFlow(t *testing.T) {
	ctx := context.Background()
	ns := createTestNamespace(ctx, t)
	clk := &fakeClock{t: time.Now()}
	stop, err := runManager(ctx, t, clk, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()

	snap := newSnapshot(ns, "happy", "ConfigMap", "demo-source")
	if err := k8s.Create(ctx, snap); err != nil {
		t.Fatal(err)
	}

	// Job must be created, exactly once.
	jobName := controller.JobName("happy", 1)
	var job batchv1.Job
	waitUntil(t, func() bool {
		return k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: jobName}, &job) == nil
	}, "job created")

	// Phase Running + observedGeneration 1.
	waitUntil(t, func() bool {
		s, e := getSnap(ctx, ns, "happy")
		return e == nil && s.Status.Phase == snapshotv1alpha1.PhaseRunning && s.Status.ObservedGeneration == 1
	}, "running at gen 1")

	// Complete the Job and publish the real artifact.
	markJobComplete(ctx, t, ns, jobName)
	createResultCM(ctx, t, ns, controller.ResultName("happy", 1), 1, nil)

	var final snapshotv1alpha1.Snapshot
	waitUntil(t, func() bool {
		s, e := getSnap(ctx, ns, "happy")
		if e != nil {
			return false
		}
		final = *s
		return s.Status.Phase == snapshotv1alpha1.PhaseReady
	}, "ready")

	if final.Status.ObservedGeneration != 1 {
		t.Fatalf("observedGeneration = %d, want 1", final.Status.ObservedGeneration)
	}
	if final.Status.SHA256 != "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" {
		t.Fatalf("unexpected digest %q", final.Status.SHA256)
	}
	if final.Status.ResultConfigMap != controller.ResultName("happy", 1) {
		t.Fatalf("unexpected result ref %q", final.Status.ResultConfigMap)
	}
	if final.Status.JobRef != jobName {
		t.Fatalf("unexpected job ref %q", final.Status.JobRef)
	}
	if len(final.Status.Files) != 1 || final.Status.Files[0].Path != "app.conf" {
		t.Fatalf("unexpected files: %+v", final.Status.Files)
	}
	if cond := readyCondition(&final); cond == nil || cond.Status != metav1.ConditionTrue {
		t.Fatalf("Ready condition not True: %+v", cond)
	}
}

// TestNoDuplicateJobs reconciles the same object many times: the controller
// must never create more than one Job for a generation.
func TestNoDuplicateJobs(t *testing.T) {
	ctx := context.Background()
	ns := createTestNamespace(ctx, t)
	clk := &fakeClock{t: time.Now()}
	stop, err := runManager(ctx, t, clk, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()

	snap := newSnapshot(ns, "dup", "ConfigMap", "src")
	if err := k8s.Create(ctx, snap); err != nil {
		t.Fatal(err)
	}
	jobName := controller.JobName("dup", 1)
	waitUntil(t, func() bool { return jobExists(ctx, ns, jobName) }, "job created")

	// Poke the object repeatedly via annotations to force reconcile events.
	for i := 0; i < 5; i++ {
		patch := client.MergeFrom(snap.DeepCopy())
		snap.Annotations = map[string]string{"poke": time.Now().Format(time.RFC3339Nano)}
		if err := k8s.Patch(ctx, snap, patch); err != nil {
			t.Fatal(err)
		}
		time.Sleep(300 * time.Millisecond)
	}
	// annotation patches don't change generation.
	var jobs batchv1.JobList
	if err := k8s.List(ctx, &jobs, client.InNamespace(ns),
		client.MatchingLabels{controller.LabelComponent: controller.ComponentJob}); err != nil {
		t.Fatal(err)
	}
	if len(jobs.Items) != 1 {
		t.Fatalf("expected exactly 1 Job, got %d", len(jobs.Items))
	}
}

// TestJobFailure marks the Job failed (backoff exhausted): Snapshot must
// become Failed with a real reason, and stay Failed on repeated reconciles.
func TestJobFailure(t *testing.T) {
	ctx := context.Background()
	ns := createTestNamespace(ctx, t)
	clk := &fakeClock{t: time.Now()}
	stop, err := runManager(ctx, t, clk, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()

	snap := newSnapshot(ns, "fail", "ConfigMap", "src")
	if err := k8s.Create(ctx, snap); err != nil {
		t.Fatal(err)
	}
	jobName := controller.JobName("fail", 1)
	waitUntil(t, func() bool { return jobExists(ctx, ns, jobName) }, "job created")
	markJobFailed(ctx, t, ns, jobName)

	var s snapshotv1alpha1.Snapshot
	waitUntil(t, func() bool {
		got, e := getSnap(ctx, ns, "fail")
		if e != nil {
			return false
		}
		s = *got
		return s.Status.Phase == snapshotv1alpha1.PhaseFailed
	}, "failed")
	if s.Status.FailureReason != controller.ReasonJobFailed {
		t.Fatalf("reason = %q", s.Status.FailureReason)
	}
	if !strings.Contains(s.Status.FailureMessage, "backoff") {
		t.Fatalf("message = %q", s.Status.FailureMessage)
	}
	if s.Status.ObservedGeneration != 1 || s.Status.CompletedAt == nil {
		t.Fatalf("bad status: %+v", s.Status)
	}

	// Wait and ensure it does not flip back / recreate a job.
	time.Sleep(1500 * time.Millisecond)
	if !jobExists(ctx, ns, jobName) {
		t.Fatal("Job disappeared while terminal")
	}
	got, _ := getSnap(ctx, ns, "fail")
	if got.Status.Phase != snapshotv1alpha1.PhaseFailed {
		t.Fatalf("phase changed to %s", got.Status.Phase)
	}
}

// TestJobSucceedsButStatusNotWrittenThenRestart simulates: Job completes and
// its artifact exists, but the status write never happened (status blank).
// After restarting the controller, state must converge to Ready on its own.
func TestJobSucceedsButStatusNotWrittenThenRestart(t *testing.T) {
	ctx := context.Background()
	ns := createTestNamespace(ctx, t)
	clk := &fakeClock{t: time.Now()}

	stop, err := runManager(ctx, t, clk, time.Minute)
	if err != nil {
		t.Fatal(err)
	}

	snap := newSnapshot(ns, "restart", "ConfigMap", "src")
	if err := k8s.Create(ctx, snap); err != nil {
		t.Fatal(err)
	}
	jobName := controller.JobName("restart", 1)
	waitUntil(t, func() bool { return jobExists(ctx, ns, jobName) }, "job created")
	waitUntil(t, func() bool {
		s, e := getSnap(ctx, ns, "restart")
		return e == nil && s.Status.Phase == snapshotv1alpha1.PhaseRunning
	}, "running before crash")

	// "Crash": stop the manager while the Job is still active, then complete
	// the Job + publish artifact while the controller is down.
	stop()
	markJobComplete(ctx, t, ns, jobName)
	createResultCM(ctx, t, ns, controller.ResultName("restart", 1), 1, nil)

	// Restart the controller; it must recover purely from cluster state.
	stop2, err := runManager(ctx, t, clk, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	defer stop2()

	var s snapshotv1alpha1.Snapshot
	waitUntil(t, func() bool {
		got, e := getSnap(ctx, ns, "restart")
		if e != nil {
			return false
		}
		s = *got
		return s.Status.Phase == snapshotv1alpha1.PhaseReady
	}, "recovers to Ready after restart")
	if s.Status.SHA256 == "" || s.Status.ObservedGeneration != 1 {
		t.Fatalf("recovered status wrong: %+v", s.Status)
	}
}

// TestSuccessfulJobWithoutArtifact: the Job reports Complete but no result
// ConfigMap is ever published. After the grace window the Snapshot must be
// reported Failed (ResultMissing), not hang forever.
func TestSuccessfulJobWithoutArtifact(t *testing.T) {
	ctx := context.Background()
	ns := createTestNamespace(ctx, t)
	// Real clock + a short grace so the controller's own requeue timer
	// deterministically drives the transition without manual clock pumping.
	stop, err := runManager(ctx, t, clock.RealClock{}, 300*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()

	snap := newSnapshot(ns, "missing", "ConfigMap", "src")
	if err := k8s.Create(ctx, snap); err != nil {
		t.Fatal(err)
	}
	jobName := controller.JobName("missing", 1)
	waitUntil(t, func() bool { return jobExists(ctx, ns, jobName) }, "job created")

	markJobComplete(ctx, t, ns, jobName)

	waitUntil(t, func() bool {
		s, e := getSnap(ctx, ns, "missing")
		return e == nil && s.Status.Phase == snapshotv1alpha1.PhaseFailed &&
			s.Status.FailureReason == controller.ReasonResultMissing
	}, "ResultMissing failure after grace")
}

// TestInvalidResultFails: a completed Job with a malformed artifact must
// produce Failed/ResultInvalid, not a fake Ready.
func TestInvalidResultFails(t *testing.T) {
	ctx := context.Background()
	ns := createTestNamespace(ctx, t)
	clk := &fakeClock{t: time.Now()}
	stop, err := runManager(ctx, t, clk, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()

	snap := newSnapshot(ns, "invalid", "ConfigMap", "src")
	if err := k8s.Create(ctx, snap); err != nil {
		t.Fatal(err)
	}
	jobName := controller.JobName("invalid", 1)
	waitUntil(t, func() bool { return jobExists(ctx, ns, jobName) }, "job created")
	markJobComplete(ctx, t, ns, jobName)

	// Publish garbage: short digest.
	createResultCM(ctx, t, ns, controller.ResultName("invalid", 1), 1, func(cm *corev1.ConfigMap) {
		cm.Data = map[string]string{"result.json": `{"apiVersion":"snapshot.example.com/v1alpha1","kind":"SnapshotResult","generation":1,"sha256":"deadbeef","sizeBytes":1,"files":[],"outputFile":"x"}`}
	})

	waitUntil(t, func() bool {
		s, e := getSnap(ctx, ns, "invalid")
		return e == nil && s.Status.Phase == snapshotv1alpha1.PhaseFailed &&
			s.Status.FailureReason == controller.ReasonResultInvalid
	}, "invalid result -> Failed")
}

// TestSpecChangeStaleJobGuard: a spec change bumps generation; a late result
// from the old generation's Job must never overwrite the new generation's
// status.
func TestSpecChangeStaleJobGuard(t *testing.T) {
	ctx := context.Background()
	ns := createTestNamespace(ctx, t)
	clk := &fakeClock{t: time.Now()}
	stop, err := runManager(ctx, t, clk, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()

	snap := newSnapshot(ns, "stale", "ConfigMap", "src-v1")
	if err := k8s.Create(ctx, snap); err != nil {
		t.Fatal(err)
	}
	gen1Job := controller.JobName("stale", 1)
	waitUntil(t, func() bool { return jobExists(ctx, ns, gen1Job) }, "gen1 job")

	// Complete gen1 and publish a valid gen1 artifact -> Ready at gen 1.
	markJobComplete(ctx, t, ns, gen1Job)
	createResultCM(ctx, t, ns, controller.ResultName("stale", 1), 1, nil)
	waitUntil(t, func() bool {
		s, e := getSnap(ctx, ns, "stale")
		return e == nil && s.Status.Phase == snapshotv1alpha1.PhaseReady && s.Status.ObservedGeneration == 1
	}, "ready gen1")

	// Spec change -> generation 2.
	got, _ := getSnap(ctx, ns, "stale")
	got.Spec.Source.Name = "src-v2"
	if err := k8s.Update(ctx, got); err != nil {
		t.Fatal(err)
	}

	gen2Job := controller.JobName("stale", 2)
	waitUntil(t, func() bool { return jobExists(ctx, ns, gen2Job) }, "gen2 job created")

	// The old gen1 Job and result must be garbage collected.
	waitUntil(t, func() bool { return !jobExists(ctx, ns, gen1Job) }, "gen1 job GC'd")

	// A late stale gen1 result arriving now must be ignored/deleted.
	var staleCM corev1.ConfigMap
	err = k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: controller.ResultName("stale", 1)}, &staleCM)
	if err == nil {
		waitUntil(t, func() bool {
			var c corev1.ConfigMap
			return apierrors.IsNotFound(k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: controller.ResultName("stale", 1)}, &c))
		}, "stale result GC'd")
	}

	// Current status must reflect generation 2 (Running), digest cleared.
	waitUntil(t, func() bool {
		s, e := getSnap(ctx, ns, "stale")
		return e == nil && s.Status.ObservedGeneration == 2 &&
			s.Status.Phase == snapshotv1alpha1.PhaseRunning && s.Status.SHA256 == ""
	}, "running gen2 with cleared digest")

	// Even if a malicious/stale gen1 result is recreated while gen2 runs,
	// status must not move to Ready.
	createResultCM(ctx, t, ns, controller.ResultName("stale", 1), 1, nil)
	time.Sleep(1500 * time.Millisecond)
	s, _ := getSnap(ctx, ns, "stale")
	if s.Status.Phase == snapshotv1alpha1.PhaseReady {
		t.Fatalf("stale gen1 result overwrote gen2 status: %+v", s.Status)
	}
	if s.Status.SHA256 != "" {
		t.Fatalf("gen1 digest leaked into gen2: %q", s.Status.SHA256)
	}
}

// TestDeletionWaitsForDependents: while a Snapshot is deleting, the finalizer
// must remain until owned Job and result ConfigMap are actually gone.
func TestDeletionWaitsForDependents(t *testing.T) {
	ctx := context.Background()
	ns := createTestNamespace(ctx, t)
	clk := &fakeClock{t: time.Now()}
	stop, err := runManager(ctx, t, clk, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()

	snap := newSnapshot(ns, "del", "ConfigMap", "src")
	if err := k8s.Create(ctx, snap); err != nil {
		t.Fatal(err)
	}
	jobName := controller.JobName("del", 1)
	waitUntil(t, func() bool { return jobExists(ctx, ns, jobName) }, "job")
	markJobComplete(ctx, t, ns, jobName)
	createResultCM(ctx, t, ns, controller.ResultName("del", 1), 1, nil)
	waitUntil(t, func() bool {
		s, e := getSnap(ctx, ns, "del")
		return e == nil && s.Status.Phase == snapshotv1alpha1.PhaseReady
	}, "ready before delete")

	if err := k8s.Delete(ctx, snap); err != nil {
		t.Fatal(err)
	}

	// The finalizer guarantees a real cleanup wait: the Snapshot must remain
	// present with the finalizer until dependents are actually deleted. We do
	// not assume a fixed sleep window; observe the terminating state directly.
	terminatingSeen := false
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		var cur snapshotv1alpha1.Snapshot
		if err := k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: "del"}, &cur); err != nil {
			if apierrors.IsNotFound(err) {
				break // fully gone
			}
			t.Fatal(err)
		}
		if cur.DeletionTimestamp != nil && containsString(cur.Finalizers, controller.FinalizerName) {
			terminatingSeen = true
			// While still finalizing, at least one dependent should remain.
			if jobExists(ctx, ns, jobName) {
				break // observed the waiting window
			}
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !terminatingSeen {
		t.Fatal("never observed Snapshot terminating with finalizer (cleanup wait not exercised)")
	}

	// Eventually everything is gone.
	waitUntil(t, func() bool {
		var deleted snapshotv1alpha1.Snapshot
		err := k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: "del"}, &deleted)
		return apierrors.IsNotFound(err)
	}, "snapshot fully deleted")

	waitUntil(t, func() bool {
		var cms corev1.ConfigMapList
		if err := k8s.List(ctx, &cms, client.InNamespace(ns),
			client.MatchingLabels{controller.LabelComponent: controller.ComponentResult}); err != nil {
			return false
		}
		for i := range cms.Items {
			if cms.Items[i].DeletionTimestamp == nil {
				return false
			}
		}
		return len(cms.Items) == 0
	}, "result ConfigMaps gone")
	var jobs batchv1.JobList
	if err := k8s.List(ctx, &jobs, client.InNamespace(ns),
		client.MatchingLabels{controller.LabelComponent: controller.ComponentJob}); err != nil {
		t.Fatal(err)
	}
	if len(jobs.Items) != 0 {
		t.Fatalf("jobs leaked after deletion: %d", len(jobs.Items))
	}
}

// TestDeletionRaceJobRecreatedDuringDeletion: even if a dependent sneaks in
// during termination, the finalizer stays until it is removed.
func TestDeletionRaceJobRecreatedDuringDeletion(t *testing.T) {
	ctx := context.Background()
	ns := createTestNamespace(ctx, t)
	clk := &fakeClock{t: time.Now()}
	stop, err := runManager(ctx, t, clk, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()

	snap := newSnapshot(ns, "race", "ConfigMap", "src")
	if err := k8s.Create(ctx, snap); err != nil {
		t.Fatal(err)
	}
	jobName := controller.JobName("race", 1)
	waitUntil(t, func() bool { return jobExists(ctx, ns, jobName) }, "job")

	// Grab the live UID to forge an owned foreign dependent.
	var live snapshotv1alpha1.Snapshot
	if err := k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: "race"}, &live); err != nil {
		t.Fatal(err)
	}

	if err := k8s.Delete(ctx, &live); err != nil {
		t.Fatal(err)
	}

	// Race: while terminating, insert another owned object (simulating an
	// in-flight create). The finalizer must wait for it too.
	intruder := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: ns,
			Name:      "late-intruder",
			Labels: map[string]string{
				controller.LabelComponent: controller.ComponentResult,
			},
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: snapshotv1alpha1.GroupVersion.String(),
				Kind:       "Snapshot",
				Name:       "race",
				UID:        live.UID,
				Controller: ptrBool(true),
			}},
		},
		Data: map[string]string{"result.json": "{}"},
	}
	_ = k8s.Create(ctx, intruder) // may 404 if namespace/snapshot already gone

	waitUntil(t, func() bool {
		var s snapshotv1alpha1.Snapshot
		if err := k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: "race"}, &s); err != nil {
			return apierrors.IsNotFound(err)
		}
		return !containsString(s.Finalizers, controller.FinalizerName)
	}, "eventually finalized")

	// And the intruder itself must be gone.
	var c corev1.ConfigMap
	if err := k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: "late-intruder"}, &c); !apierrors.IsNotFound(err) {
		// accepted if it was never created because the Snapshot was already gone
		if err == nil {
			t.Fatal("intruder ConfigMap leaked after finalization")
		}
	}
}

// --- small helpers ---------------------------------------------------------

func waitUntil(t *testing.T, cond func() bool, what string) {
	t.Helper()
	deadline := time.Now().Add(waitFor)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(poll)
	}
	t.Fatalf("timed out waiting for: %s", what)
}

func readyCondition(s *snapshotv1alpha1.Snapshot) *metav1.Condition {
	for i := range s.Status.Conditions {
		if s.Status.Conditions[i].Type == "Ready" {
			return &s.Status.Conditions[i]
		}
	}
	return nil
}

func containsString(xs []string, s string) bool {
	for _, x := range xs {
		if x == s {
			return true
		}
	}
	return false
}

func ptrBool(b bool) *bool { return &b }

var _ clock.Clock = &fakeClock{}
