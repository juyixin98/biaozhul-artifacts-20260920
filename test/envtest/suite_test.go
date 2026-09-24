// Package envtest contains integration tests that run the real reconciler and
// the real admission webhooks against a real apiserver (envtest: etcd +
// kube-apiserver binaries). Nothing is mocked at the API layer.
package envtest

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	"k8s.io/client-go/rest"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/webhook"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	quotav1alpha1 "github.com/biaozhul/quota-reserver/api/v1alpha1"
	"github.com/biaozhul/quota-reserver/internal/cert"
	"github.com/biaozhul/quota-reserver/internal/controller"
	podwebhook "github.com/biaozhul/quota-reserver/internal/webhook"
)

var (
	cfg         *rest.Config
	testEnv     *envtest.Environment
	k8sClient   client.Client // direct, uncached client for test assertions
	scheme      = runtime.NewScheme()
	managerSeq  atomic.Int32
)

func TestMain(m *testing.M) {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(quotav1alpha1.AddToScheme(scheme))

	testEnv = &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "config", "crd")},
		ErrorIfCRDPathMissing: true,
		WebhookInstallOptions: envtest.WebhookInstallOptions{
			Paths: []string{filepath.Join("..", "..", "config", "webhook")},
		},
	}
	var err error
	cfg, err = testEnv.Start()
	if err != nil {
		panic(fmt.Sprintf("starting envtest: %v", err))
	}
	k8sClient, err = client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		panic(err)
	}

	code := m.Run()
	if err := testEnv.Stop(); err != nil {
		fmt.Fprintf(os.Stderr, "stopping envtest: %v\n", err)
	}
	os.Exit(code)
}

var extraWebhookPort = int32(21443)

// startManager starts a real manager with the reconciler (and optionally the
// webhooks wired to the envtest-installed webhook configuration) and returns
// a stop function that shuts it down completely.
func startManager(t *testing.T, withWebhooks bool) (stop func()) {
	t.Helper()
	opts := ctrl.Options{
		Scheme:                  scheme,
		Metrics:                 metricsserver.Options{BindAddress: "0"},
		HealthProbeBindAddress:  "0",
		LeaderElection:          false,
		LeaderElectionNamespace: "default",
	}
	if withWebhooks {
		opts.WebhookServer = webhook.NewServer(webhook.Options{
			Host:    testEnv.WebhookInstallOptions.LocalServingHost,
			Port:    testEnv.WebhookInstallOptions.LocalServingPort,
			CertDir: testEnv.WebhookInstallOptions.LocalServingCertDir,
		})
	} else {
		// The webhook server starts even with no registered handlers and
		// requires serving certs on disk; give it a throwaway self-signed pair.
		certDir := t.TempDir()
		_, certPEM, keyPEM, err := cert.Generate("unused", "unused")
		if err != nil {
			t.Fatalf("generating throwaway webhook cert: %v", err)
		}
		if err := os.WriteFile(filepath.Join(certDir, "tls.crt"), certPEM, 0o644); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(certDir, "tls.key"), keyPEM, 0o600); err != nil {
			t.Fatal(err)
		}
		opts.WebhookServer = webhook.NewServer(webhook.Options{
			Port:    int(atomic.AddInt32(&extraWebhookPort, 1)),
			CertDir: certDir,
		})
	}
	mgr, err := ctrl.NewManager(cfg, opts)
	if err != nil {
		t.Fatalf("creating manager: %v", err)
	}
	if err := (&controller.ResourceRequestReconciler{
		Client:         mgr.GetClient(),
		Scheme:         mgr.GetScheme(),
		ControllerName: fmt.Sprintf("resourcerequest-%d", managerSeq.Add(1)),
	}).SetupWithManager(mgr); err != nil {
		t.Fatalf("setting up reconciler: %v", err)
	}
	if err := quotav1alpha1.SetupResourceRequestWebhook(mgr); err != nil {
		t.Fatalf("setting up request webhook: %v", err)
	}
	if withWebhooks {
		mgr.GetWebhookServer().Register(podwebhook.PodValidatePath, &admission.Webhook{
			Handler: &podwebhook.PodAdmission{
				Client:  k8sClient,
				Decoder: admission.NewDecoder(scheme),
			},
		})
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		if err := mgr.Start(ctx); err != nil {
			fmt.Fprintf(os.Stderr, "manager stopped with error: %v\n", err)
		}
	}()
	if !mgr.GetCache().WaitForCacheSync(ctx) {
		t.Fatal("cache did not sync")
	}
	var once sync.Once
	stop = func() { once.Do(func() { cancel(); <-done }) }
	t.Cleanup(stop)
	return stop
}

// newNamespace creates a test namespace. managed=true labels it so the
// admission webhooks apply (their namespaceSelector requires the label);
// tests without a running webhook server must use managed=false.
func newNamespace(t *testing.T, managed bool) string {
	t.Helper()
	name := fmt.Sprintf("test-%d", time.Now().UnixNano())
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}}
	if managed {
		ns.Labels = map[string]string{quotav1alpha1.LabelManagedBy: quotav1alpha1.ManagedByValue}
	}
	if err := k8sClient.Create(context.Background(), ns); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	t.Cleanup(func() { _ = k8sClient.Delete(context.Background(), ns) })
	return name
}

func createPool(t *testing.T, ns string, cpuMilli, memBytes int64) {
	t.Helper()
	pool := &quotav1alpha1.QuotaPool{
		ObjectMeta: metav1.ObjectMeta{Name: "default", Namespace: ns},
		Spec:       quotav1alpha1.QuotaPoolSpec{CPUMilli: cpuMilli, MemoryBytes: memBytes},
	}
	if err := k8sClient.Create(context.Background(), pool); err != nil {
		t.Fatalf("creating pool: %v", err)
	}
}

func createRequest(t *testing.T, ns, name string, cpuMilli, memBytes, ttl int64) *quotav1alpha1.ResourceRequest {
	t.Helper()
	rr := &quotav1alpha1.ResourceRequest{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Spec: quotav1alpha1.ResourceRequestSpec{
			Pool: "default", CPUMilli: cpuMilli, MemoryBytes: memBytes, TTLSeconds: ttl,
		},
	}
	if err := k8sClient.Create(context.Background(), rr); err != nil {
		t.Fatalf("creating request %s: %v", name, err)
	}
	return rr
}

func getRequest(t *testing.T, ns, name string) *quotav1alpha1.ResourceRequest {
	t.Helper()
	var rr quotav1alpha1.ResourceRequest
	if err := k8sClient.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: name}, &rr); err != nil {
		t.Fatalf("getting request %s: %v", name, err)
	}
	return &rr
}

func getPool(t *testing.T, ns string) *quotav1alpha1.QuotaPool {
	t.Helper()
	var pool quotav1alpha1.QuotaPool
	if err := k8sClient.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: "default"}, &pool); err != nil {
		t.Fatalf("getting pool: %v", err)
	}
	return &pool
}

func phaseOf(t *testing.T, ns, name string) quotav1alpha1.Phase {
	t.Helper()
	return getRequest(t, ns, name).Status.Phase
}

// eventually polls cond until it returns true or the timeout elapses.
func eventually(t *testing.T, timeout time.Duration, msg string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("condition not met within %s: %s", timeout, msg)
}

func waitPhase(t *testing.T, ns, name string, phase quotav1alpha1.Phase, timeout time.Duration) {
	t.Helper()
	eventually(t, timeout, fmt.Sprintf("request %s to reach phase %s", name, phase),
		func() bool { return phaseOf(t, ns, name) == phase })
}

// countPhases returns the number of requests per phase in the namespace.
func countPhases(t *testing.T, ns string) map[quotav1alpha1.Phase]int {
	t.Helper()
	var list quotav1alpha1.ResourceRequestList
	if err := k8sClient.List(context.Background(), &list, client.InNamespace(ns)); err != nil {
		t.Fatalf("listing requests: %v", err)
	}
	counts := map[quotav1alpha1.Phase]int{}
	for _, rr := range list.Items {
		counts[rr.Status.Phase]++
	}
	return counts
}

// checkInvariant verifies the core consistency relation:
// pool.used == sum(pool ledger) == sum(spec of Reserved/Bound requests).
func checkInvariant(t *testing.T, ns string) {
	t.Helper()
	pool := getPool(t, ns)
	var ledgerCPU, ledgerMem int64
	for _, a := range pool.Status.Allocations {
		ledgerCPU += a.CPUMilli
		ledgerMem += a.MemoryBytes
	}
	if pool.Status.UsedCPUMilli != ledgerCPU || pool.Status.UsedMemoryBytes != ledgerMem {
		t.Fatalf("pool counters drifted from ledger: used=%dm/%dB ledger=%dm/%dB",
			pool.Status.UsedCPUMilli, pool.Status.UsedMemoryBytes, ledgerCPU, ledgerMem)
	}
	var list quotav1alpha1.ResourceRequestList
	if err := k8sClient.List(context.Background(), &list, client.InNamespace(ns)); err != nil {
		t.Fatal(err)
	}
	var activeCPU, activeMem int64
	activeUIDs := map[types.UID]bool{}
	for _, rr := range list.Items {
		if rr.Status.Phase == quotav1alpha1.PhaseReserved || rr.Status.Phase == quotav1alpha1.PhaseBound {
			activeCPU += rr.Spec.CPUMilli
			activeMem += rr.Spec.MemoryBytes
			activeUIDs[rr.UID] = true
		}
	}
	if ledgerCPU != activeCPU || ledgerMem != activeMem {
		t.Fatalf("ledger=%dm/%dB != active reservations=%dm/%dB",
			ledgerCPU, ledgerMem, activeCPU, activeMem)
	}
	for _, a := range pool.Status.Allocations {
		if !activeUIDs[a.RequestUID] {
			t.Fatalf("ledger holds allocation for non-active request UID %s", a.RequestUID)
		}
	}
	if len(pool.Status.Allocations) != len(activeUIDs) {
		t.Fatalf("ledger has %d entries but %d requests are active",
			len(pool.Status.Allocations), len(activeUIDs))
	}
}
