package controller_test

import (
	"context"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	"snapshotcontroller/internal/controller"
)

// restartableHarness is a harness whose manager can be stopped and started
// repeatedly while cluster state persists. Methods used by tests are defined
// on *testHarness; this embeds the same shape by sharing a fresh harness per
// restart, keyed to the same namespace.
type restartableHarness struct {
	*testHarness
}

// startManagerStopped creates a harness but does not run a manager yet,
// leaving the namespace prepared. Call restart to boot the controller.
func startManagerStopped(t *testing.T, ns string) *restartableHarness {
	t.Helper()

	k8s, err := client.New(cfg, client.Options{Scheme: testScheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	_ = k8s.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}})

	h := &testHarness{t: t, k8s: k8s, ns: ns, cancel: func() {}}
	rh := &restartableHarness{testHarness: h}
	return rh
}

// restart boots (or reboots) the manager for this harness's namespace.
func (rh *restartableHarness) restart(_ context.Context) {
	t := rh.t
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
			Image:           "kindest/node:v1.30.10",
			ImagePullPolicy: corev1.PullNever,
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
	rh.cancel = cancel
	rh.reconciler = r
	time.Sleep(100 * time.Millisecond)
}
