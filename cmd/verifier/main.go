// Command verifier independently re-computes a Snapshot digest from the live
// source ConfigMap and checks it against the recorded status.
//
// It uses the same internal/digest algorithm but reads everything fresh from
// the API, so a successful run proves: (1) status.digest really corresponds to
// the current source bytes, (2) every per-file hash in the manifest matches,
// (3) the recorded digest is sha256(manifest). Exits non-zero on any mismatch.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	_ "k8s.io/client-go/plugin/pkg/client/auth"
	"k8s.io/client-go/tools/clientcmd"
	"sigs.k8s.io/controller-runtime/pkg/client"

	snapshotv1alpha1 "snapshotcontroller/api/v1alpha1"
	"snapshotcontroller/internal/digest"
)

func main() {
	var name, namespace, kubeconfig, contextName string
	flag.StringVar(&name, "snapshot", "", "Snapshot name (required)")
	flag.StringVar(&namespace, "namespace", "default", "Namespace")
	flag.StringVar(&kubeconfig, "kubeconfig", "", "Path to kubeconfig (defaults to KUBECONFIG/~/.kube/config)")
	flag.StringVar(&contextName, "context", "", "kubeconfig context to use (defaults to current)")
	flag.Parse()
	if name == "" {
		fmt.Fprintln(os.Stderr, "--snapshot is required")
		os.Exit(2)
	}

	scheme := runtime.NewScheme()
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(snapshotv1alpha1.AddToScheme(scheme))

	loading := clientcmd.NewDefaultClientConfigLoadingRules()
	if kubeconfig != "" {
		loading.ExplicitPath = kubeconfig
	}
	overrides := &clientcmd.ConfigOverrides{}
	if contextName != "" {
		overrides.CurrentContext = contextName
	}
	restCfg, err := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(loading, overrides).ClientConfig()
	if err != nil {
		fmt.Fprintf(os.Stderr, "kubeconfig: %v\n", err)
		os.Exit(1)
	}
	c, err := client.New(restCfg, client.Options{Scheme: scheme})
	if err != nil {
		fmt.Fprintf(os.Stderr, "client: %v\n", err)
		os.Exit(1)
	}
	ctx := context.Background()

	var snap snapshotv1alpha1.Snapshot
	if err := c.Get(ctx, client.ObjectKey{Namespace: namespace, Name: name}, &snap); err != nil {
		fmt.Fprintf(os.Stderr, "get snapshot: %v\n", err)
		os.Exit(1)
	}
	if snap.Status.Phase != snapshotv1alpha1.PhaseReady {
		fmt.Fprintf(os.Stderr, "snapshot %s is %s, not Ready (observedGen=%d)\n",
			name, snap.Status.Phase, snap.Status.ObservedGeneration)
		os.Exit(1)
	}

	var src corev1.ConfigMap
	if err := c.Get(ctx, client.ObjectKey{Namespace: namespace, Name: snap.Spec.SourceConfigMap}, &src); err != nil {
		fmt.Fprintf(os.Stderr, "get source configmap: %v\n", err)
		os.Exit(1)
	}

	files := digest.Files{}
	for k, v := range src.Data {
		files[k] = []byte(v)
	}
	recomputed, err := digest.Compute(files, snap.Spec.SubPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "recompute failed: %v\n", err)
		os.Exit(1)
	}

	fail := false
	check := func(label, got, want string) {
		if got != want {
			fmt.Printf("MISMATCH %s:\n  status:   %q\n  recomputed/expected: %q\n", label, got, want)
			fail = true
		} else {
			fmt.Printf("ok  %s = %s\n", label, got)
		}
	}

	check("observedGeneration", fmt.Sprintf("%d", snap.Status.ObservedGeneration), fmt.Sprintf("%d", snap.Generation))
	check("digest", snap.Status.Digest, recomputed.Digest)
	check("fileCount", fmt.Sprintf("%d", snap.Status.FileCount), fmt.Sprintf("%d", recomputed.FileCount))
	check("totalBytes", fmt.Sprintf("%d", snap.Status.TotalBytes), fmt.Sprintf("%d", recomputed.TotalBytes))
	check("algorithm", snap.Status.Algorithm, recomputed.Algorithm)

	// Independently validate the recorded manifest too.
	rootFromManifest, err := digest.Verify(snap.Status.Manifest, files)
	if err != nil {
		fmt.Printf("MISMATCH manifest verification: %v\n", err)
		fail = true
	} else {
		check("manifest->digest", snap.Status.Digest, rootFromManifest)
	}

	fmt.Println("--- recomputed manifest ---")
	fmt.Print(recomputed.Manifest)
	if recomputed.Manifest != "" {
		fmt.Println()
	}

	if fail {
		os.Exit(1)
	}
	fmt.Printf("VERIFIED: snapshot %q digest matches live source %q\n", name, snap.Spec.SourceConfigMap)
}
