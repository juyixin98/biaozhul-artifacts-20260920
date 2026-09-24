// Command manager runs the config-distributor controller and, optionally, the
// chaos-injection and validation admission webhooks.
package main

import (
	"bytes"
	"context"
	"flag"
	"net/http"
	"os"
	"time"

	admissionregistrationv1 "k8s.io/api/admissionregistration/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/klog/v2"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/webhook"

	configv1alpha1 "github.com/example/config-distributor/api/v1alpha1"
	"github.com/example/config-distributor/internal/controller"
	"github.com/example/config-distributor/internal/failinject"
	"github.com/example/config-distributor/internal/failinject/certs"
)

var (
	scheme   = runtime.NewScheme()
	setupLog = ctrl.Log.WithName("setup")
)

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(configv1alpha1.AddToScheme(scheme))
	utilruntime.Must(failinject.AddToScheme(scheme))
}

func main() {
	var (
		metricsAddr        string
		probeAddr          string
		enableChaosWebhook bool
		enableValidator    bool
		webhookPort        int
		certDir            string
		webhookServiceName string
		chaosConfigName    string
		retryInterval      time.Duration
	)
	flag.StringVar(&metricsAddr, "metrics-bind-address", ":8080", "metrics endpoint address")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "probe endpoint address")
	flag.BoolVar(&enableChaosWebhook, "chaos-webhook", true, "enable the chaos-injection admission webhook")
	flag.BoolVar(&enableValidator, "validate-webhook", true, "enable the ConfigSnapshot validation webhook")
	flag.IntVar(&webhookPort, "webhook-port", 9443, "admission webhook port")
	flag.StringVar(&certDir, "cert-dir", "/tmp/k8s-webhook-server/serving-certs", "webhook serving cert dir")
	flag.StringVar(&webhookServiceName, "webhook-service-name",
		"config-distributor-webhook.config-system.svc", "DNS name baked into the serving cert")
	flag.StringVar(&chaosConfigName, "chaos-webhook-config-name",
		"config-distributor-chaos", "ValidatingWebhookConfiguration name for the chaos webhook")
	flag.DurationVar(&retryInterval, "retry-interval", 10*time.Second,
		"requeue interval while targets are failing")
	klog.InitFlags(nil)
	flag.Parse()

	ctrl.SetLogger(klog.NewKlogr())

	webhooksEnabled := enableChaosWebhook || enableValidator
	if webhooksEnabled {
		if err := certs.EnsureCerts(certDir, []string{webhookServiceName}, nil); err != nil {
			setupLog.Error(err, "ensuring serving certs")
			os.Exit(1)
		}
	}

	// Build the webhook server but do NOT start its TLS listener: the manager
	// only adds the server to its runnables once GetWebhookServer() is called,
	// and the HTTP handlers are registered via Register(path, handler) below.
	webhookServer := webhook.NewServer(webhook.Options{
		Port:    webhookPort,
		CertDir: certDir,
	})

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: metricsAddr},
		HealthProbeBindAddress: probeAddr,
		WebhookServer:          webhookServer,
		LeaderElection:         false,
	})
	if err != nil {
		setupLog.Error(err, "unable to start manager")
		os.Exit(1)
	}

	reconciler := &controller.Reconciler{
		Client:        mgr.GetClient(),
		Scheme:        mgr.GetScheme(),
		Recorder:      mgr.GetEventRecorderFor("config-distributor"),
		RetryInterval: retryInterval,
	}
	if err := reconciler.SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to set up reconciler")
		os.Exit(1)
	}

	if webhooksEnabled {
		matcher := failinject.NewMatcher()
		syncer := &controller.PolicySyncer{
			Client:  mgr.GetClient(),
			Scheme:  mgr.GetScheme(),
			Matcher: matcher,
		}
		// The syncer loads policy/namespace state inside Start; until then the
		// matcher allows everything, which is the safe default.
		if err := mgr.Add(syncer); err != nil {
			setupLog.Error(err, "add policy syncer")
			os.Exit(1)
		}
		// GetWebhookServer() is what actually adds the server to the manager's
		// runnables; merely passing it in Options does not start its TLS
		// listener. Register our handlers on it so it binds :9443.
		srv := mgr.GetWebhookServer()
		registerWebhooks(srv, matcher, enableChaosWebhook, enableValidator)

		bootstrap := &webhookBootstrap{
			client:  mgr.GetClient(),
			certDir: certDir,
			configs: webhookConfigNames(chaosConfigName, enableChaosWebhook, enableValidator),
		}
		if err := mgr.Add(bootstrap); err != nil {
			setupLog.Error(err, "add webhook bootstrap")
			os.Exit(1)
		}
	}

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		setupLog.Error(err, "set up health check")
		os.Exit(1)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		setupLog.Error(err, "set up ready check")
		os.Exit(1)
	}

	setupLog.Info("starting manager", "chaosWebhook", enableChaosWebhook, "validator", enableValidator)
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		setupLog.Error(err, "manager exited with error")
		os.Exit(1)
	}
}

func registerWebhooks(srv webhook.Server, matcher *failinject.Matcher, chaos, validator bool) {
	wh := failinject.NewWebhook(matcher)
	if chaos {
		srv.Register("/inject", http.HandlerFunc(wh.HandleInject))
		setupLog.Info("chaos webhook registered at /inject")
	}
	if validator {
		srv.Register("/validate-configsnapshot", http.HandlerFunc(wh.HandleValidate))
		setupLog.Info("validation webhook registered at /validate-configsnapshot")
	}
}

// webhookConfigNames returns the ValidatingWebhookConfiguration objects that
// must carry the generated CA bundle.
func webhookConfigNames(base string, chaos, validator bool) []string {
	var names []string
	if chaos {
		names = append(names, base)
	}
	if validator {
		names = append(names, "config-distributor-validate")
	}
	return names
}

// webhookBootstrap patches the generated caBundle into one or more
// ValidatingWebhookConfigurations once the manager's client is warm.
type webhookBootstrap struct {
	client  client.Client
	certDir string
	configs []string
}

func (b *webhookBootstrap) Start(ctx context.Context) error {
	ca, err := certs.LoadCAPEM(b.certDir)
	if err != nil {
		return err
	}
	// Continuously reconcile the CA bundle rather than patching once: the webhook
	// config can be re-created (or applied with an empty bundle) after startup,
	// and it must converge back to trusting our CA without a manager restart.
	ticker := time.NewTicker(3 * time.Second)
	defer ticker.Stop()
	sync := func() {
		for _, name := range b.configs {
			b.patchOne(ctx, name, ca)
		}
	}
	sync()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			sync()
		}
	}
}

func (b *webhookBootstrap) patchOne(ctx context.Context, name string, ca []byte) {
	vwc := &admissionregistrationv1.ValidatingWebhookConfiguration{}
	if err := b.client.Get(ctx, client.ObjectKey{Name: name}, vwc); err != nil {
		setupLog.Info("waiting for webhook config", "name", name)
		return
	}
	allMatch := len(vwc.Webhooks) > 0
	for _, wh := range vwc.Webhooks {
		if !bytes.Equal(wh.ClientConfig.CABundle, ca) {
			allMatch = false
			break
		}
	}
	if allMatch {
		return
	}
	for i := range vwc.Webhooks {
		vwc.Webhooks[i].ClientConfig.CABundle = ca
	}
	if err := b.client.Update(ctx, vwc); err != nil {
		setupLog.Info("webhook config patch retry", "name", name, "error", err.Error())
		return
	}
	setupLog.Info("patched caBundle into webhook config", "name", name)
}
