// Command manager runs the resource-quota reservation controller and webhooks.
package main

import (
	"flag"
	"os"

	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	_ "k8s.io/client-go/plugin/pkg/client/auth"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/webhook"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	quota "resourcequota-reservation/api/v1alpha1"
	"resourcequota-reservation/internal/controller"
	reswebhook "resourcequota-reservation/internal/webhook"
)

var scheme = runtime.NewScheme()

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(quota.AddToScheme(scheme))
}

func main() {
	var (
		metricsAddr          string
		probeAddr            string
		enableLeaderElection bool
		webhookPort          int
		certDir              string
	)
	flag.StringVar(&metricsAddr, "metrics-bind-address", ":8080", "metrics endpoint bind address")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "health probe bind address")
	flag.BoolVar(&enableLeaderElection, "leader-elect", false, "enable leader election")
	flag.IntVar(&webhookPort, "webhook-port", 9443, "admission webhook port")
	flag.StringVar(&certDir, "cert-dir", "/tmp/k8s-webhook-server/serving-certs", "directory containing tls.crt/tls.key")
	opts := zap.Options{Development: true}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))
	setupLog := ctrl.Log.WithName("setup")

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: metricsAddr},
		HealthProbeBindAddress: probeAddr,
		LeaderElection:         enableLeaderElection,
		LeaderElectionID:       "quota-reservation-leader.example.com",
		WebhookServer: webhook.NewServer(webhook.Options{
			Port:    webhookPort,
			CertDir: certDir,
		}),
	})
	if err != nil {
		setupLog.Error(err, "unable to create manager")
		os.Exit(1)
	}

	if err := (&controller.ClaimReconciler{
		Client:                mgr.GetClient(),
		Scheme:                mgr.GetScheme(),
		MaxReservationRetries: controller.DefaultMaxReservationRetries,
	}).SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to set up claim controller")
		os.Exit(1)
	}

	hookServer := mgr.GetWebhookServer()
	dec := admission.NewDecoder(mgr.GetScheme())
	hookServer.Register("/validate-quota-example-com-v1alpha1-resourceclaim",
		&webhook.Admission{Handler: &reswebhook.ClaimValidator{Client: mgr.GetClient(), Decoder: dec}})
	hookServer.Register("/validate-core-v1-pod",
		&webhook.Admission{Handler: &reswebhook.PodValidator{Client: mgr.GetClient(), Decoder: dec}})

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up health check")
		os.Exit(1)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up ready check")
		os.Exit(1)
	}

	setupLog.Info("starting manager", "webhookPort", webhookPort)
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		setupLog.Error(err, "problem running manager")
		os.Exit(1)
	}
}
