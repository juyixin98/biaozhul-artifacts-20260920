// Package integration contains envtest-backed end-to-end tests. They start a
// real kube-apiserver + etcd, install the Task CRD with its conversion
// webhook pointed at an in-process TLS server, and exercise the exact path a
// client takes: admission defaulting/validation, server-side conversion and
// etcd storage.
package integration

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	ctrllog "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	"sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/webhook"

	v1 "github.com/example/crd-migration-compat/api/v1"
	v1alpha1 "github.com/example/crd-migration-compat/api/v1alpha1"
	migconv "github.com/example/crd-migration-compat/internal/conversion"
	migwebhook "github.com/example/crd-migration-compat/internal/webhook"
)

var (
	testEnv     *envtest.Environment
	restCfg     *rest.Config
	adminClient client.Client

	// kubeconfigPath is populated once the apiserver is up; the storage
	// migration CLI tests point --kubeconfig at it.
	kubeconfigPath string

	// slowHook is nil by default. The slow-conversion test installs a hook
	// that blocks past the apiserver webhook budget and restarts the suite's
	// webhook process via a dedicated server (see conversion_timeout_test.go).
	slowHook migconv.Hooks
)

func TestMain(m *testing.M) {
	ctrllog.SetLogger(zap.New(zap.UseDevMode(false)))

	code := runIntegrationSuite(m)
	os.Exit(code)
}

func runIntegrationSuite(m *testing.M) int {
	root := repoRoot()

	testEnv = &envtest.Environment{
		// CRDs are installed manually after webhook setup: envtest.Start would
		// otherwise create them before IgnoreSchemeConvertible and the local
		// CA/host:port are populated, leaving the CRD without a usable
		// conversion clientConfig.
		WebhookInstallOptions: envtest.WebhookInstallOptions{Paths: []string{filepath.Join(root, "config", "webhook")}},
		BinaryAssetsDirectory: os.Getenv("KUBEBUILDER_ASSETS"),
	}

	var err error
	restCfg, err = testEnv.Start()
	if err != nil {
		fmt.Fprintf(os.Stderr, "start envtest: %v\n", err)
		return 1
	}

	s := integratedScheme()

	// 1. Allocate the local TLS listener/CA and install MWC/VWC (clientConfig
	// rewritten to localhost).
	// 2. Install CRDs once, now that modifyConversionWebhooks can point the
	// CRD conversion clientConfig at the local TLS endpoint.
	// IgnoreSchemeConvertible lets the CRD keep its conversion webhook even
	// though our Go types do not implement the in-scheme Hub/Spoke
	// interfaces (conversion is intentionally unstructured).
	testEnv.WebhookInstallOptions.IgnoreSchemeConvertible = true
	if err := testEnv.WebhookInstallOptions.Install(restCfg); err != nil {
		fmt.Fprintf(os.Stderr, "install webhooks: %v\n", err)
		return teardown(1)
	}
	if _, err := envtest.InstallCRDs(restCfg, envtest.CRDInstallOptions{
		Paths:              []string{filepath.Join(root, "config", "crd")},
		Scheme:             s,
		WebhookOptions:     testEnv.WebhookInstallOptions,
		ErrorIfPathMissing: true,
	}); err != nil {
		fmt.Fprintf(os.Stderr, "install CRDs with conversion webhook: %v\n", err)
		return teardown(1)
	}

	mgr, err := ctrl.NewManager(restCfg, ctrl.Options{
		Scheme: s,
		// Bind the metrics listener to an ephemeral port so running the suite
		// on a host that already serves :8080 does not take the manager (and
		// its webhook server) down.
		Metrics: server.Options{BindAddress: "127.0.0.1:0"},
		WebhookServer: webhook.NewServer(webhook.Options{
			Port:    testEnv.WebhookInstallOptions.LocalServingPort,
			Host:    testEnv.WebhookInstallOptions.LocalServingHost,
			CertDir: testEnv.WebhookInstallOptions.LocalServingCertDir,
		}),
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "new manager: %v\n", err)
		return teardown(1)
	}
	mgr.GetWebhookServer().Register("/", migwebhook.NewMux(migwebhook.Options{
		Converter:         migconv.NewConverter(slowHook),
		ConversionTimeout: 8 * time.Second,
	}))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		if err := mgr.Start(ctx); err != nil {
			ctrllog.Log.Info("manager exited", "error", err.Error())
		}
	}()

	if !mgr.GetCache().WaitForCacheSync(ctx) {
		fmt.Fprintln(os.Stderr, "cache sync failed")
		return teardown(1)
	}
	adminClient = mgr.GetClient()

	// Namespace used by all tests.
	ns := &corev1.Namespace{}
	ns.Name = "itest"
	if err := adminClient.Create(ctx, ns); err != nil {
		fmt.Fprintf(os.Stderr, "create namespace: %v\n", err)
		return teardown(1)
	}

	dir, err := os.MkdirTemp("", "crd-mig-it-")
	if err != nil {
		fmt.Fprintf(os.Stderr, "tempdir: %v\n", err)
		return teardown(1)
	}
	kubeconfigPath = filepath.Join(dir, "kubeconfig")
	if err := writeKubeconfig(kubeconfigPath, restCfg); err != nil {
		fmt.Fprintf(os.Stderr, "write kubeconfig: %v\n", err)
		return teardown(1)
	}

	// Build the real storage migration CLI once; tests invoke it as a
	// subprocess exactly like an operator would in production.
	storageMigrateBin = filepath.Join(dir, "storage-migrate")
	build := exec.Command("go", "build", "-o", storageMigrateBin,
		"./cmd/storage-migrate")
	build.Dir = repoRoot()
	if out, err := build.CombinedOutput(); err != nil {
		fmt.Fprintf(os.Stderr, "build storage-migrate: %v\n%s\n", err, out)
		return teardown(1)
	}

	code := m.Run()
	return teardown(code)
}

func teardown(code int) int {
	if testEnv != nil {
		_ = testEnv.Stop()
	}
	return code
}

func integratedScheme() *runtime.Scheme {
	s := runtime.NewScheme()
	_ = clientgoscheme.AddToScheme(s)
	_ = v1.AddToScheme(s)
	_ = v1alpha1.AddToScheme(s)
	return s
}

func repoRoot() string {
	abs, _ := filepath.Abs(".")
	// test/integration -> two levels up.
	return filepath.Join(abs, "..", "..")
}
