package controller_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/scheme"
	"k8s.io/klog/v2"
	"k8s.io/utils/clock"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	ctrlmetrics "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	snapshotv1alpha1 "snapshotcontroller/api/v1alpha1"
	"snapshotcontroller/internal/controller"
)

var (
	testEnv    *envtest.Environment
	k8s        client.Client
	testScheme = runtime.NewScheme()
)

// fakeClock allows deterministic control of the result-grace window.
type fakeClock struct{ t time.Time }

func (f *fakeClock) Now() time.Time                   { return f.t }
func (f *fakeClock) Since(ts time.Time) time.Duration { return f.t.Sub(ts) }
func (f *fakeClock) After(d time.Duration) <-chan time.Time {
	c := make(chan time.Time, 1)
	c <- f.t.Add(d)
	return c
}
func (f *fakeClock) NewTimer(d time.Duration) clock.Timer {
	c := make(chan time.Time, 1)
	return &fakeTimer{c: c}
}
func (f *fakeClock) Sleep(d time.Duration)                 { f.t = f.t.Add(d) }
func (f *fakeClock) Tick(d time.Duration) <-chan time.Time { return nil }

type fakeTimer struct{ c chan time.Time }

func (f *fakeTimer) C() <-chan time.Time        { return f.c }
func (f *fakeTimer) Stop() bool                 { return false }
func (f *fakeTimer) Reset(d time.Duration) bool { return false }

func TestMain(m *testing.M) {
	log.SetLogger(zap.New(zap.WriteTo(io.Discard), zap.UseDevMode(true)))
	ctrl.SetLogger(log.Log)
	klog.SetLogger(log.Log)

	testEnv = &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "config", "crd", "bases")},
		ErrorIfCRDPathMissing: true,
	}

	cfg, err := testEnv.Start()
	if err != nil {
		panic(fmt.Sprintf("failed to start envtest: %v", err))
	}

	s := testScheme
	if err := scheme.AddToScheme(s); err != nil {
		panic(err)
	}
	if err := snapshotv1alpha1.AddToScheme(s); err != nil {
		panic(err)
	}
	k8s, err = client.New(cfg, client.Options{Scheme: s})
	if err != nil {
		panic(err)
	}

	code := m.Run()
	_ = testEnv.Stop()
	os.Exit(code)
}

// runManager starts a manager+reconciler in a goroutine and returns a stop
// func that cancels it and waits for it to stop. Used to exercise real
// controller restarts.
// managerInstance disambiguates controllers when a test restarts the manager
// within one process (controller names must be globally unique).
var managerInstance int64

func runManager(ctx context.Context, t *testing.T, clk clock.Clock, grace time.Duration) (func(), error) {
	n := atomic.AddInt64(&managerInstance, 1)
	name := fmt.Sprintf("%s-%d", t.Name(), n)
	mgr, err := ctrl.NewManager(testEnv.Config, ctrl.Options{
		Scheme:         testScheme,
		Metrics:        ctrlmetrics.Options{BindAddress: "0"},
		LeaderElection: false,
		Logger:         log.FromContext(ctx),
	})
	if err != nil {
		return nil, err
	}
	if err := (&controller.Reconciler{
		Client:   mgr.GetClient(),
		Scheme:   mgr.GetScheme(),
		Recorder: mgr.GetEventRecorderFor("test"),
		Clock:    clk,
		Config: controller.Config{
			ResultGrace:         grace,
			RequeueWhileRunning: 500 * time.Millisecond,
		},
	}).SetupWithName(mgr, name); err != nil {
		return nil, err
	}
	inner, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() {
		_ = mgr.Start(inner)
		close(done)
	}()
	// Wait for cache to be ready so tests do not race initial list.
	if !mgr.GetCache().WaitForCacheSync(inner) {
		cancel()
		return nil, fmt.Errorf("cache sync failed")
	}
	return func() {
		cancel()
		<-done
	}, nil
}

func createTestNamespace(ctx context.Context, t *testing.T) string {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: fmt.Sprintf("snap-%d", time.Now().UnixNano())}}
	if err := k8s.Create(ctx, ns); err != nil {
		t.Fatalf("create namespace: %v", err)
	}
	t.Cleanup(func() {
		_ = k8s.Delete(context.Background(), ns)
	})
	return ns.Name
}

func newSnapshot(ns, name string, sourceKind, sourceName string) *snapshotv1alpha1.Snapshot {
	bl := int32(2)
	return &snapshotv1alpha1.Snapshot{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name},
		Spec: snapshotv1alpha1.SnapshotSpec{
			Source:       snapshotv1alpha1.SnapshotSource{Kind: sourceKind, Name: sourceName},
			OutputName:   "snapshot.tar.gz",
			BackoffLimit: &bl,
		},
	}
}

func getSnap(ctx context.Context, ns, name string) (*snapshotv1alpha1.Snapshot, error) {
	var s snapshotv1alpha1.Snapshot
	err := k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &s)
	return &s, err
}

// markJobComplete simulates a worker Job finishing successfully.
func markJobComplete(ctx context.Context, t *testing.T, ns, name string) {
	t.Helper()
	var job batchv1.Job
	if err := k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &job); err != nil {
		t.Fatalf("get job %s: %v", name, err)
	}
	now := metav1.Now()
	job.Status.Succeeded = 1
	job.Status.Conditions = []batchv1.JobCondition{{
		Type: batchv1.JobComplete, Status: corev1.ConditionTrue,
		LastTransitionTime: now, LastProbeTime: now,
	}}
	if err := k8s.Status().Update(ctx, &job); err != nil {
		t.Fatalf("mark complete: %v", err)
	}
}

func markJobFailed(ctx context.Context, t *testing.T, ns, name string) {
	t.Helper()
	var job batchv1.Job
	if err := k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &job); err != nil {
		t.Fatalf("get job %s: %v", name, err)
	}
	now := metav1.Now()
	backoff := int32(3)
	job.Status.Failed = backoff
	job.Status.Conditions = []batchv1.JobCondition{{
		Type: batchv1.JobFailed, Status: corev1.ConditionTrue,
		Reason: "BackoffLimitExceeded", Message: "job reached backoff limit",
		LastTransitionTime: now, LastProbeTime: now,
	}}
	if err := k8s.Status().Update(ctx, &job); err != nil {
		t.Fatalf("mark failed: %v", err)
	}
}

// validResult builds the worker's result.json payload.
func validResult(generation int64) map[string]string {
	res := controller.WorkerResult{
		APIVersion: "snapshot.example.com/v1alpha1",
		Kind:       "SnapshotResult",
		Generation: generation,
		Source:     "ConfigMap/demo",
		OutputFile: "snapshot.tar.gz",
		SHA256:     "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
		SizeBytes:  256,
		Files: []controller.ResultFile{{
			Path:   "app.conf",
			Size:   10,
			SHA256: "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
		}},
		ComputedAt: time.Now().UTC().Format(time.RFC3339),
	}
	b, _ := json.Marshal(res)
	return map[string]string{"result.json": string(b)}
}

func createResultCM(ctx context.Context, t *testing.T, ns, name string, gen int64, mutate func(*corev1.ConfigMap)) {
	t.Helper()
	// Look up the owner Snapshot to set a real controller ownerReference,
	// mirroring what the worker does in the cluster.
	snapName := snapNameFromResult(name, gen)
	var owner snapshotv1alpha1.Snapshot
	if err := k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: snapName}, &owner); err != nil {
		t.Fatalf("get owner snapshot %s: %v", snapName, err)
	}
	controllerRef := true
	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: ns, Name: name,
			Labels: map[string]string{
				"snapshot.example.com/component":  "snapshot-result",
				"snapshot.example.com/generation": fmt.Sprintf("%d", gen),
			},
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: snapshotv1alpha1.GroupVersion.String(),
				Kind:       "Snapshot",
				Name:       owner.Name,
				UID:        owner.UID,
				Controller: &controllerRef,
			}},
		},
		Data: validResult(gen),
	}
	if mutate != nil {
		mutate(cm)
	}
	if err := k8s.Create(ctx, cm); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("create result cm: %v", err)
	}
}

// snapNameFromResult reverses ResultName("<snap>", gen) for test ownership.
func snapNameFromResult(resultName string, gen int64) string {
	suffix := fmt.Sprintf("-g%d", gen)
	base := strings.TrimPrefix(resultName, controller.ResultNamePrefix)
	return strings.TrimSuffix(base, suffix)
}

func mustGetJob(ctx context.Context, t *testing.T, ns, name string) *batchv1.Job {
	t.Helper()
	var job batchv1.Job
	if err := k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &job); err != nil {
		t.Fatalf("get job %s: %v", name, err)
	}
	return &job
}

func jobExists(ctx context.Context, ns, name string) bool {
	var job batchv1.Job
	return k8s.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &job) == nil
}

var _ = client.MatchingLabels{}
