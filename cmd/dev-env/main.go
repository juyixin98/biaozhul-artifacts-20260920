// Command dev-env starts a complete local development environment: a real
// kube-apiserver + etcd (via envtest), the Task CRD with its conversion
// webhook, the mutating/validating admission webhooks, and an in-process
// HTTPS webhook server serving all five endpoints. It then writes a kubeconfig
// and prints the commands to exercise it.
//
// It is the local-startup counterpart of test/integration — same wiring,
// long-lived instead of test-scoped. Requirements:
//
//	export KUBEBUILDER_ASSETS="$(setup-envtest use 1.31.0 -p path)"
//	go run ./cmd/dev-env
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	"sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/webhook"

	v1 "github.com/example/crd-migration-compat/api/v1"
	v1alpha1 "github.com/example/crd-migration-compat/api/v1alpha1"
	migconv "github.com/example/crd-migration-compat/internal/conversion"
	migwebhook "github.com/example/crd-migration-compat/internal/webhook"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "dev-env: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	root := repoRoot()

	te := &envtest.Environment{
		// CRDs installed after webhook CA/port are ready, same as the tests.
		WebhookInstallOptions: envtest.WebhookInstallOptions{
			Paths: []string{filepath.Join(root, "config", "webhook")},
		},
	}
	cfg, err := te.Start()
	if err != nil {
		return fmt.Errorf("start envtest (KUBEBUILDER_ASSETS set?): %w", err)
	}
	defer func() { _ = te.Stop() }()

	s := runtime.NewScheme()
	must(clientgoscheme.AddToScheme(s))
	must(v1.AddToScheme(s))
	must(v1alpha1.AddToScheme(s))

	te.WebhookInstallOptions.IgnoreSchemeConvertible = true
	if err := te.WebhookInstallOptions.Install(cfg); err != nil {
		return fmt.Errorf("install admission webhooks: %w", err)
	}
	if _, err := envtest.InstallCRDs(cfg, envtest.CRDInstallOptions{
		Paths:              []string{filepath.Join(root, "config", "crd")},
		Scheme:             s,
		WebhookOptions:     te.WebhookInstallOptions,
		ErrorIfPathMissing: true,
	}); err != nil {
		return fmt.Errorf("install CRD: %w", err)
	}

	mgr, err := ctrl.NewManager(cfg, ctrl.Options{
		Scheme:  s,
		Metrics: server.Options{BindAddress: "127.0.0.1:0"},
		WebhookServer: webhook.NewServer(webhook.Options{
			Port:    te.WebhookInstallOptions.LocalServingPort,
			Host:    te.WebhookInstallOptions.LocalServingHost,
			CertDir: te.WebhookInstallOptions.LocalServingCertDir,
		}),
	})
	if err != nil {
		return err
	}
	mgr.GetWebhookServer().Register("/", migwebhook.NewMux(migwebhook.Options{
		Converter:         migconv.NewConverter(migconv.Hooks{}),
		ConversionTimeout: 10 * time.Second,
	}))

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go func() {
		if err := mgr.Start(ctx); err != nil {
			fmt.Fprintf(os.Stderr, "manager: %v\n", err)
		}
	}()
	if !mgr.GetCache().WaitForCacheSync(ctx) {
		return fmt.Errorf("cache sync failed")
	}

	if err := mgr.GetClient().Create(ctx, &corev1.Namespace{
		ObjectMeta: metav1.ObjectMeta{Name: "default"},
	}); err != nil && !apierrors.IsAlreadyExists(err) {
		return fmt.Errorf("ensure default namespace: %w", err)
	}

	kubeconfig := filepath.Join(os.TempDir(), "crd-migrate-dev-kubeconfig")
	if err := writeKubeconfig(kubeconfig, cfg); err != nil {
		return err
	}

	printReady(kubeconfig, cfg.Host, te.WebhookInstallOptions.LocalServingPort)
	<-ctx.Done()
	fmt.Println("\nshutting down")
	return nil
}

func printReady(kubeconfig, apiServer string, webhookPort int) {
	fmt.Printf(`
dev-env is ready.

  API server : %s
  webhook    : https://127.0.0.1:%d/convert (+ /mutate-*, /validate-*, /healthz)
  kubeconfig : %s

Use it:
  export KUBECONFIG=%s
  kubectl apply -f test/fixtures/task-v1alpha1.yaml
  kubectl get tasks.v1alpha1.migration.example.io nightly-backup -o yaml
  kubectl get tasks.migration.example.io nightly-backup -o yaml      # served as v1
  kubectl apply -f test/fixtures/task-v1.yaml
  kubectl get tasks.v1alpha1.migration.example.io latency-sensitive # expected: sub-second conversion error
  go run ./cmd/storage-migrate --record-file ./migration-records.jsonl

Press Ctrl-C to stop.
`, apiServer, webhookPort, kubeconfig, kubeconfig)
}

func writeKubeconfig(path string, cfg *rest.Config) error {
	caFile := path + "-ca.crt"
	if err := os.WriteFile(caFile, cfg.TLSClientConfig.CAData, 0o600); err != nil {
		return err
	}
	cluster := clientcmdapi.NewCluster()
	cluster.Server = cfg.Host
	cluster.CertificateAuthority = caFile
	auth := clientcmdapi.NewAuthInfo()
	// envtest authenticates the "admin" user via a client certificate; older
	// versions used bearer/basic auth, so support all three shapes.
	if len(cfg.TLSClientConfig.CertData) > 0 {
		certFile, keyFile := path+"-client.crt", path+"-client.key"
		if err := os.WriteFile(certFile, cfg.TLSClientConfig.CertData, 0o600); err != nil {
			return err
		}
		if err := os.WriteFile(keyFile, cfg.TLSClientConfig.KeyData, 0o600); err != nil {
			return err
		}
		auth.ClientCertificate = certFile
		auth.ClientKey = keyFile
	}
	if cfg.BearerToken != "" {
		auth.Token = cfg.BearerToken
	}
	if u := cfg.Username; u != "" {
		auth.Username = u
		auth.Password = cfg.Password
	}
	kc := clientcmdapi.NewConfig()
	kc.Clusters["dev-env"] = cluster
	kc.AuthInfos["dev-env-user"] = auth
	c := clientcmdapi.NewContext()
	c.Cluster, c.AuthInfo = "dev-env", "dev-env-user"
	kc.Contexts["dev-env"] = c
	kc.CurrentContext = "dev-env"
	return clientcmd.WriteToFile(*kc, path)
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}

// repoRoot resolves the repository root. When run with `go run ./cmd/dev-env`
// the working directory is the root; for a built binary we walk up from the
// executable until we find config/crd.
func repoRoot() string {
	if _, err := os.Stat(filepath.Join("config", "crd")); err == nil {
		abs, _ := filepath.Abs(".")
		return abs
	}
	exe, err := os.Executable()
	if err != nil {
		return "."
	}
	dir := filepath.Dir(exe)
	for i := 0; i < 6; i++ {
		if _, err := os.Stat(filepath.Join(dir, "config", "crd")); err == nil {
			return dir
		}
		dir = filepath.Dir(dir)
	}
	return "."
}
