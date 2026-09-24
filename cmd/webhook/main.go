// Command webhook serves the Timer conversion, mutating and validating
// webhooks over HTTPS. Cert/key files are required (see README for the local
// mkcert/openssl snippet; in-cluster use cert-manager).
package main

import (
	"flag"
	"os"

	"github.com/example/crd-migration-demo/internal/admission"
	"github.com/example/crd-migration-demo/internal/conversion"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/config"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	"sigs.k8s.io/controller-runtime/pkg/manager"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/webhook"
)

func main() {
	var (
		port     int
		certFile string
		keyFile  string
	)
	flag.IntVar(&port, "port", 9443, "HTTPS port for webhook endpoints")
	flag.StringVar(&certFile, "tls-cert-file", "/tmp/k8s-webhook-server/serving-certs/tls.crt", "TLS certificate")
	flag.StringVar(&keyFile, "tls-key-file", "/tmp/k8s-webhook-server/serving-certs/tls.key", "TLS private key")
	flag.Parse()

	log.SetLogger(zap.New(zap.UseDevMode(true)))
	logger := log.Log

	restCfg, err := config.GetConfig()
	if err != nil {
		logger.Error(err, "unable to get kubeconfig/rest config")
		os.Exit(1)
	}

	mgr, err := manager.New(restCfg, manager.Options{
		Metrics: metricsserver.Options{BindAddress: "0"},
		WebhookServer: webhook.NewServer(webhook.Options{
			Port:    port,
			CertDir: dirOf(certFile),
		}),
	})
	if err != nil {
		logger.Error(err, "unable to start manager")
		os.Exit(1)
	}

	srv := mgr.GetWebhookServer()
	srv.Register("/convert", conversion.NewHandler(logger.WithName("conversion")))
	srv.Register("/mutate-v1-timer", admission.NewHandler(logger.WithName("admission")))
	srv.Register("/validate-v1-timer", admission.NewHandler(logger.WithName("admission")))

	logger.Info("starting webhook server", "port", port)
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		logger.Error(err, "manager terminated with error")
		os.Exit(1)
	}
}

// dirOf returns the directory portion of a file path, defaulting to the
// controller-runtime convention when the path has no directory.
func dirOf(file string) string {
	for i := len(file) - 1; i >= 0; i-- {
		if file[i] == '/' {
			return file[:i]
		}
	}
	return "/tmp/k8s-webhook-server/serving-certs"
}
