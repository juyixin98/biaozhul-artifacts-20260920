package main

import (
	"flag"
	"os"
	"time"

	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/webhook"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"

	quotav1alpha1 "github.com/biaozhul/quota-reserver/api/v1alpha1"
	"github.com/biaozhul/quota-reserver/internal/cert"
	"github.com/biaozhul/quota-reserver/internal/controller"
	podwebhook "github.com/biaozhul/quota-reserver/internal/webhook"
)

var (
	scheme = runtime.NewScheme()
)

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(quotav1alpha1.AddToScheme(scheme))
}

func main() {
	var metricsAddr, probeAddr, certDir string
	var enableLeaderElection bool
	flag.StringVar(&metricsAddr, "metrics-bind-address", ":8080",
		"The address the metric endpoint binds to.")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081",
		"The address the probe endpoint binds to.")
	flag.StringVar(&certDir, "webhook-cert-dir", "/tmp/k8s-webhook-server/serving-certs",
		"Directory the webhook server reads tls.crt/tls.key from.")
	flag.BoolVar(&enableLeaderElection, "leader-elect", true,
		"Enable leader election for the controller manager.")
	opts := zap.Options{}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))
	setupLog := ctrl.Log.WithName("setup")

	namespace := os.Getenv("POD_NAMESPACE")
	if namespace == "" {
		namespace = "quota-system"
	}

	cfg := ctrl.GetConfigOrDie()
	ctx := ctrl.SetupSignalHandler()

	// Direct (uncached) client for PKI provisioning and webhook decisions.
	directClient, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		setupLog.Error(err, "unable to create direct client")
		os.Exit(1)
	}

	// Provision the webhook PKI before the manager starts the webhook server:
	// generate a real self-signed CA + serving cert into a Secret, write the
	// serving pair to certDir, and publish the CA bundle to the webhook config.
	pkiOpts := cert.Options{
		Namespace:         namespace,
		ServiceName:       cert.ServiceName,
		SecretName:        cert.SecretName,
		WebhookConfigName: cert.WebhookConfigName,
		CertDir:           certDir,
	}
	if err := cert.EnsurePKI(ctx, directClient, pkiOpts); err != nil {
		setupLog.Error(err, "unable to provision webhook PKI")
		os.Exit(1)
	}
	// Self-healing: a later `kubectl apply` of the install manifest must not
	// be able to leave a bad/missing caBundle in place. Reconcile the bundle
	// periodically while the manager runs.
	go func() {
		ticker := time.NewTicker(60 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if err := cert.EnsurePKI(ctx, directClient, pkiOpts); err != nil {
					setupLog.Error(err, "reconciling webhook PKI")
				}
			}
		}
	}()

	mgr, err := ctrl.NewManager(cfg, ctrl.Options{
		Scheme: scheme,
		Metrics: metricsserver.Options{
			BindAddress: metricsAddr,
		},
		WebhookServer: webhook.NewServer(webhook.Options{
			Port:    9443,
			CertDir: certDir,
		}),
		HealthProbeBindAddress: probeAddr,
		LeaderElection:         enableLeaderElection,
		LeaderElectionID:       "quota-reserver.quota.biaozhu.dev",
	})
	if err != nil {
		setupLog.Error(err, "unable to start manager")
		os.Exit(1)
	}

	if err := (&controller.ResourceRequestReconciler{
		Client: mgr.GetClient(),
		Scheme: mgr.GetScheme(),
	}).SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to create controller", "controller", "ResourceRequest")
		os.Exit(1)
	}

	if err := quotav1alpha1.SetupResourceRequestWebhook(mgr); err != nil {
		setupLog.Error(err, "unable to create webhook", "webhook", "ResourceRequest")
		os.Exit(1)
	}
	mgr.GetWebhookServer().Register(podwebhook.PodValidatePath, &admission.Webhook{
		Handler: &podwebhook.PodAdmission{
			Client:  directClient,
			Decoder: admission.NewDecoder(scheme),
		},
	})

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up health check")
		os.Exit(1)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up ready check")
		os.Exit(1)
	}
	// The pod must not appear Ready until the webhook TLS listener is up;
	// otherwise Service traffic (and the restart window) can hit a pod whose
	// admission server is not accepting connections yet.
	if err := mgr.AddReadyzCheck("webhook", mgr.GetWebhookServer().StartedChecker()); err != nil {
		setupLog.Error(err, "unable to set up webhook ready check")
		os.Exit(1)
	}

	setupLog.Info("starting manager", "namespace", namespace)
	if err := mgr.Start(ctx); err != nil {
		setupLog.Error(err, "problem running manager")
		os.Exit(1)
	}
}
