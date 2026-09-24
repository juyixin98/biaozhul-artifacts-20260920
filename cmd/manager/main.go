// Command manager runs the recoverable snapshot controller.
package main

import (
	"flag"
	"os"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	_ "k8s.io/client-go/plugin/pkg/client/auth"
	"k8s.io/klog/v2"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	ctrlwebhook "sigs.k8s.io/controller-runtime/pkg/webhook"

	snapshotv1alpha1 "snapshotcontroller/api/v1alpha1"
	"snapshotcontroller/internal/controller"
)

var scheme = runtime.NewScheme()

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(snapshotv1alpha1.AddToScheme(scheme))
	utilruntime.Must(batchv1.AddToScheme(scheme))
	utilruntime.Must(corev1.AddToScheme(scheme))
}

func main() {
	var (
		metricsAddr          string
		probeAddr            string
		enableLeaderElection bool
		jobImage             string
		jobPullPolicy        string
		workerSA             string
		backoffLimit         int
	)
	flag.StringVar(&metricsAddr, "metrics-bind-address", ":8080", "The address the metric endpoint binds to.")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "The address the probe endpoint binds to.")
	flag.BoolVar(&enableLeaderElection, "leader-elect", false,
		"Enable leader election for controller manager.")
	flag.StringVar(&jobImage, "job-image", "localhost:5000/snapshot-worker:dev",
		"Image used by the worker Job (must contain bash, coreutils and kubectl).")
	flag.StringVar(&jobPullPolicy, "job-image-pull-policy", "IfNotPresent",
		"ImagePullPolicy for the worker Job.")
	flag.StringVar(&workerSA, "worker-service-account", "snapshot-worker",
		"ServiceAccount assigned to worker Jobs.")
	flag.IntVar(&backoffLimit, "job-backoff-limit", 1,
		"BackoffLimit for worker Jobs.")
	opts := zap.Options{Development: true}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))
	klog.SetLogger(ctrl.Log)

	cfg, err := ctrl.GetConfig()
	if err != nil {
		ctrl.Log.Error(err, "unable to load kubeconfig")
		os.Exit(1)
	}

	mgr, err := ctrl.NewManager(cfg, ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: metricsAddr},
		WebhookServer:          ctrlwebhook.NewServer(ctrlwebhook.Options{Port: 9443}),
		HealthProbeBindAddress: probeAddr,
		LeaderElection:         enableLeaderElection,
		LeaderElectionID:       "snapshot-controller.snapshot.example.com",
	})
	if err != nil {
		setupExit(err, "unable to start manager")
	}

	if err = (&controller.Reconciler{
		Client:   mgr.GetClient(),
		Scheme:   mgr.GetScheme(),
		Recorder: mgr.GetEventRecorderFor("snapshot-controller"),
		JobCfg: controller.JobImageConfig{
			Image:           jobImage,
			ImagePullPolicy: corev1.PullPolicy(jobPullPolicy),
			ServiceAccount:  workerSA,
			BackoffLimit:    int32(backoffLimit),
		},
	}).SetupWithManager(mgr); err != nil {
		setupExit(err, "unable to create controller")
	}

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		setupExit(err, "unable to set up health check")
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		setupExit(err, "unable to set up ready check")
	}

	ctrl.Log.Info("starting snapshot-controller manager", "jobImage", jobImage)
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		setupExit(err, "problem running manager")
	}
}

func setupExit(err error, msg string) {
	ctrl.Log.Error(err, msg)
	os.Exit(1)
}
