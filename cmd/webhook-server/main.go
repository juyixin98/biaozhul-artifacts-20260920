// Command webhook-server serves the Task CRD conversion and admission
// webhooks over HTTPS. It is a pure webhook process: it registers no
// controllers and talks to no API server, so it can run next to any cluster
// (kind, k3d, envtest) that trusts its serving certificate.
//
// Configuration is entirely by flag/env so the same binary is used in local
// development (hack/local-webhook.sh) and in integration tests.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	"go.uber.org/zap/zapcore"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	"sigs.k8s.io/controller-runtime/pkg/webhook"

	migconv "github.com/example/crd-migration-compat/internal/conversion"
	migwebhook "github.com/example/crd-migration-compat/internal/webhook"
)

func main() {
	var (
		port              int
		certDir           string
		conversionTimeout time.Duration
	)
	flag.IntVar(&port, "port", envInt("WEBHOOK_PORT", 9443), "HTTPS port")
	flag.StringVar(&certDir, "cert-dir", envStr("WEBHOOK_CERT_DIR", "/tmp/k8s-webhook-server/serving-certs"),
		"directory containing tls.crt and tls.key")
	flag.DurationVar(&conversionTimeout, "conversion-timeout",
		envDuration("CONVERSION_TIMEOUT", 10*time.Second),
		"per-object conversion deadline (0 disables the local deadline)")
	opts := zap.Options{Development: true, TimeEncoder: zapcore.ISO8601TimeEncoder}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))
	log := ctrl.Log.WithName("webhook-server")

	converter := migconv.NewConverter(migconv.Hooks{})
	mux := migwebhook.NewMux(migwebhook.Options{
		Converter:         converter,
		ConversionTimeout: conversionTimeout,
	})

	srv := webhook.NewServer(webhook.Options{
		Port:    port,
		CertDir: certDir,
	})
	// NewMux returns a single http.Handler with every route (/convert,
	// /mutate-*, /validate-*, /healthz) already wired; mount it at the root so
	// conversion and admission share one TLS listener.
	srv.Register("/", mux)

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	log.Info("starting webhook server", "port", port, "certDir", certDir)
	if err := srv.Start(ctx); err != nil {
		fmt.Fprintf(os.Stderr, "webhook server exited: %v\n", err)
		os.Exit(1)
	}
}

func envStr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		var n int
		if _, err := fmt.Sscanf(v, "%d", &n); err == nil {
			return n
		}
	}
	return def
}

func envDuration(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
