package integration

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/webhook"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	quota "resourcequota-reservation/api/v1alpha1"
	"resourcequota-reservation/internal/controller"
	reswebhook "resourcequota-reservation/internal/webhook"
)

var (
	testEnv *envtest.Environment
	scheme  = runtime.NewScheme()
	// directClient bypasses the manager cache so assertions see the latest
	// etcd state without waiting for cache propagation.
	directClient client.Client
)

func init() {
	_ = clientgoscheme.AddToScheme(scheme)
	_ = quota.AddToScheme(scheme)
}

func TestMain(m *testing.M) {
	log.SetLogger(zap.New(zap.WriteTo(os.Stderr), zap.UseDevMode(true)))

	assets := findEnvtestAssets()

	testEnv = &envtest.Environment{
		CRDDirectoryPaths: []string{
			filepath.Join("..", "..", "config", "crd"),
		},
		WebhookInstallOptions: envtest.WebhookInstallOptions{
			Paths: []string{filepath.Join("..", "..", "config", "webhook", "manifests-envtest.yaml")},
		},
		BinaryAssetsDirectory: assets,
	}

	cfg, err := testEnv.Start()
	if err != nil {
		panic("failed to start envtest: " + err.Error())
	}

	directClient, err = client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		panic(err)
	}

	code := m.Run()
	_ = testEnv.Stop()
	os.Exit(code)
}

// findEnvtestAssets locates kube-apiserver/etcd binaries: $KUBEBUILDER_ASSETS
// first, then the local setup-envtest cache used by this machine's setup.
func findEnvtestAssets() string {
	if p := os.Getenv("KUBEBUILDER_ASSETS"); p != "" {
		return p
	}
	home, _ := os.UserHomeDir()
	matches, _ := filepath.Glob(filepath.Join(home, "go", "bin", "envtest-bin", "k8s", "1.30.*-linux-*"))
	if len(matches) > 0 {
		return matches[0]
	}
	if out, err := exec.Command("setup-envtest", "use", "-i", "-p", "path", "1.30.x").Output(); err == nil {
		return strings.TrimSpace(string(out))
	}
	return "" // envtest will fail with a clear download hint
}

// startedManager is a running manager instance plus its cancel function.
type startedManager struct {
	cancel context.CancelFunc
}

var managerSerial int64

// startManager runs a fresh manager (controller + webhooks) and returns once
// its webhook server is accepting connections. Call stop() to stop it.
func startManager(t *testing.T) *startedManager {
	t.Helper()
	wo := testEnv.WebhookInstallOptions

	mgr, err := ctrl.NewManager(testEnv.Config, ctrl.Options{
		Scheme:  scheme,
		Metrics: metricsserver.Options{BindAddress: "0"},
		WebhookServer: webhook.NewServer(webhook.Options{
			Host:    wo.LocalServingHost,
			Port:    wo.LocalServingPort,
			CertDir: wo.LocalServingCertDir,
		}),
	})
	if err != nil {
		t.Fatalf("new manager: %v", err)
	}
	n := atomic.AddInt64(&managerSerial, 1)
	if err := (&controller.ClaimReconciler{
		Client:         mgr.GetClient(),
		Scheme:         scheme,
		ControllerName: fmt.Sprintf("resourceclaim-%d", n),
	}).SetupWithManager(mgr); err != nil {
		t.Fatalf("setup controller: %v", err)
	}
	dec := admission.NewDecoder(scheme)
	srv := mgr.GetWebhookServer()
	srv.Register("/validate-quota-example-com-v1alpha1-resourceclaim",
		&webhook.Admission{Handler: &reswebhook.ClaimValidator{Client: mgr.GetClient(), Decoder: dec}})
	srv.Register("/validate-core-v1-pod",
		&webhook.Admission{Handler: &reswebhook.PodValidator{Client: mgr.GetClient(), Decoder: dec}})

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		if err := mgr.Start(ctx); err != nil {
			t.Logf("manager exited: %v", err)
		}
	}()

	// Wait for the webhook server (envtest rewrote the webhook clientConfig
	// to point at this port at environment start).
	check := mgr.GetWebhookServer().StartedChecker()
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		if err := check(nil); err == nil {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if err := check(nil); err != nil {
		cancel()
		t.Fatalf("webhook server never became ready: %v", err)
	}
	return &startedManager{cancel: cancel}
}

func (s *startedManager) stop() { s.cancel() }

// --- helpers ---------------------------------------------------------------

func newNamespace(t *testing.T, name string) string {
	t.Helper()
	ns := &corev1.Namespace{}
	ns.Name = name
	if err := directClient.Create(context.Background(), ns); err != nil {
		t.Fatalf("create namespace %s: %v", name, err)
	}
	t.Cleanup(func() {
		_ = directClient.Delete(context.Background(), ns)
	})
	return name
}

func createPool(t *testing.T, ns, cpuCap, memCap string) *quota.ReservationPool {
	t.Helper()
	pool := &quota.ReservationPool{}
	pool.Namespace = ns
	pool.Name = quota.PoolName
	pool.Spec = quota.PoolSpec{CPUCapacity: cpuCap, MemoryCapacity: memCap}
	if err := directClient.Create(context.Background(), pool); err != nil {
		t.Fatalf("create pool: %v", err)
	}
	t.Cleanup(func() {
		_ = directClient.Delete(context.Background(), pool)
	})
	return pool
}

func createClaim(t *testing.T, ns, name, cpu, mem, ttl string) *quota.ResourceClaim {
	t.Helper()
	c := &quota.ResourceClaim{}
	c.Namespace = ns
	c.Name = name
	c.Spec = quota.ResourceClaimSpec{
		CPU:    cpu,
		Memory: mem,
		TTL:    parseDur(t, ttl),
	}
	if err := directClient.Create(context.Background(), c); err != nil {
		t.Fatalf("create claim %s: %v", name, err)
	}
	t.Cleanup(func() {
		_ = directClient.Delete(context.Background(), c)
	})
	return c
}

func getClaim(t *testing.T, ns, name string) *quota.ResourceClaim {
	t.Helper()
	c := &quota.ResourceClaim{}
	if err := directClient.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: name}, c); err != nil {
		t.Fatalf("get claim %s: %v", name, err)
	}
	return c
}

func getPool(t *testing.T, ns string) *quota.ReservationPool {
	t.Helper()
	p := &quota.ReservationPool{}
	if err := directClient.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: quota.PoolName}, p); err != nil {
		t.Fatalf("get pool: %v", err)
	}
	return p
}

func eventually(t *testing.T, timeout time.Duration, f func() bool, msg string, args ...any) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if f() {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("condition never met: %s", fmt.Sprintf(msg, args...))
}
