package controller_test

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	snapshotv1alpha1 "snapshotcontroller/api/v1alpha1"
	"snapshotcontroller/internal/controller"
)

var (
	testEnv    *envtest.Environment
	cfg        *rest.Config
	testScheme = runtime.NewScheme()
)

func TestMain(m *testing.M) {
	log.SetLogger(zap.New(zap.UseDevMode(true)))

	_ = clientgoscheme.AddToScheme(testScheme)
	_ = snapshotv1alpha1.AddToScheme(testScheme)

	testEnv = &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "config", "crd", "bases")},
		ErrorIfCRDPathMissing: true,
	}

	var err error
	cfg, err = testEnv.Start()
	if err != nil {
		panic("failed to start envtest: " + err.Error())
	}

	code := m.Run()
	_ = testEnv.Stop()
	if code != 0 {
		panic("tests failed")
	}
}

// testHarness bundles a manager and its client for one test/namespace.
type testHarness struct {
	t          *testing.T
	k8s        client.Client
	cancel     context.CancelFunc
	ns         string
	reconciler *controller.Reconciler
}

// startManager runs a real controller manager in-process for ns. Calling it
// again after h.stop() against the same envtest apiserver simulates a
// controller restart with all cluster state intact.
func startManager(t *testing.T, ns string) *testHarness {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())

	mgr, err := ctrl.NewManager(cfg, ctrl.Options{
		Scheme:  testScheme,
		Metrics: metricsserver.Options{BindAddress: "0"},
	})
	if err != nil {
		cancel()
		t.Fatalf("new manager: %v", err)
	}

	r := &controller.Reconciler{
		Client: mgr.GetClient(),
		Scheme: testScheme,
		JobCfg: controller.JobImageConfig{
			Image:           "localhost:5000/snapshot-worker:dev",
			ImagePullPolicy: corev1.PullNever, // no pods actually run in envtest
			ServiceAccount:  "snapshot-worker",
			BackoffLimit:    1,
		},
		RequeueWhileRunning: 50 * time.Millisecond,
		RequeuePending:      50 * time.Millisecond,
	}
	if err := r.SetupWithManager(mgr); err != nil {
		cancel()
		t.Fatalf("setup: %v", err)
	}

	go func() {
		if err := mgr.Start(ctx); err != nil {
			t.Logf("manager exited: %v", err)
		}
	}()

	k8s, err := client.New(cfg, client.Options{Scheme: testScheme})
	if err != nil {
		cancel()
		t.Fatal(err)
	}

	_ = k8s.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}})

	h := &testHarness{t: t, k8s: k8s, cancel: cancel, ns: ns, reconciler: r}
	t.Cleanup(h.stop)
	return h
}

func (h *testHarness) stop() {
	h.cancel()
	time.Sleep(50 * time.Millisecond)
}

// ---- test object builders ----

func newSnapshot(name, ns, source string) *snapshotv1alpha1.Snapshot {
	return &snapshotv1alpha1.Snapshot{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Spec:       snapshotv1alpha1.SnapshotSpec{SourceConfigMap: source},
	}
}

func sourceConfigMap(name, ns string, data map[string]string) *corev1.ConfigMap {
	return &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns}, Data: data}
}

func (h *testHarness) key(name string) types.NamespacedName {
	return types.NamespacedName{Namespace: h.ns, Name: name}
}

func (h *testHarness) getSnapshot(ctx context.Context, name string) *snapshotv1alpha1.Snapshot {
	var s snapshotv1alpha1.Snapshot
	if err := h.k8s.Get(ctx, h.key(name), &s); err != nil {
		h.t.Fatalf("get snapshot %s: %v", name, err)
	}
	return &s
}

func (h *testHarness) getJob(ctx context.Context, name string) *batchv1.Job {
	var j batchv1.Job
	if err := h.k8s.Get(ctx, types.NamespacedName{Namespace: h.ns, Name: name}, &j); err != nil {
		h.t.Fatalf("get job %s: %v", name, err)
	}
	return &j
}

func (h *testHarness) listJobs(ctx context.Context, snapshotName string) []batchv1.Job {
	var list batchv1.JobList
	if err := h.k8s.List(ctx, &list, client.InNamespace(h.ns),
		client.MatchingLabels{"snapshot.example.com/snapshot": snapshotName}); err != nil {
		h.t.Fatalf("list jobs: %v", err)
	}
	return list.Items
}

func (h *testHarness) listResultConfigMaps(ctx context.Context, snapshotName string) []corev1.ConfigMap {
	var list corev1.ConfigMapList
	if err := h.k8s.List(ctx, &list, client.InNamespace(h.ns),
		client.MatchingLabels{"snapshot.example.com/snapshot": snapshotName}); err != nil {
		h.t.Fatalf("list configmaps: %v", err)
	}
	return list.Items
}
