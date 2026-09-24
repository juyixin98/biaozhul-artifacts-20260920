// Command storage-migrator rewrites every Timer object so the API server
// re-encodes it under the current storage version. It emits a JSON record
// file (--record) and exits non-zero if any object failed; see
// docs/ROLLBACK.md for the recovery procedure.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/example/crd-migration-demo/internal/migrator"
	apiextclientset "k8s.io/apiextensions-apiserver/pkg/client/clientset/clientset"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client/config"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
)

func main() {
	var (
		direction  string
		recordPath string
		timeout    time.Duration
	)
	flag.StringVar(&direction, "direction", "up", "migration direction: up (v1alpha1->v1) or down (v1->v1alpha1)")
	flag.StringVar(&recordPath, "record", "migration-record.json", "path to write the JSON migration record")
	flag.DurationVar(&timeout, "timeout", 5*time.Minute, "overall deadline for the sweep")
	flag.Parse()

	logf.SetLogger(zap.New(zap.UseDevMode(true)))
	logger := logf.Log

	if direction != "up" && direction != "down" {
		fmt.Fprintf(os.Stderr, "--direction must be 'up' or 'down', got %q\n", direction)
		os.Exit(2)
	}

	cfg, err := config.GetConfig()
	if err != nil {
		logger.Error(err, "loading kubeconfig")
		os.Exit(1)
	}
	dyn, err := dynamic.NewForConfig(cfg)
	if err != nil {
		logger.Error(err, "building dynamic client")
		os.Exit(1)
	}
	ext, err := apiextclientset.NewForConfig(cfg)
	if err != nil {
		logger.Error(err, "building apiextensions client")
		os.Exit(1)
	}
	_ = scheme.Scheme // ensure client-go types are linked

	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	rec, err := migrator.Run(ctx, migrator.Options{
		ExtClient:  ext,
		Dynamic:    dyn,
		Direction:  direction,
		RecordPath: recordPath,
	})
	if rec != nil {
		logger.Info("sweep finished",
			"total", rec.Total, "migrated", rec.Migrated,
			"skipped", rec.Skipped, "failed", rec.Failed,
			"record", recordPath)
	}
	if err != nil {
		logger.Error(err, "migration incomplete")
		os.Exit(1)
	}
}
