// Command storage-migrate performs a storage-version migration of Task custom
// objects after the CRD storage version flips from v1alpha1 to v1.
//
// It implements the "rewrite every object" operation recommended by
// https://kubernetes.io/docs/tasks/extend-kubernetes/custom-resources/custom-resource-definition-versioning/#upgrade-existing-objects-to-a-new-stored-version:
//
//  1. GET/LIST each object. The API server converts it on the fly to the
//     currently served (storage) version and the conversion webhook applies
//     v1alpha1 -> v1 semantics, including the preserve annotation.
//  2. UPDATE the object unchanged (same resourceVersion). etcd now stores
//     v1 bytes.
//  3. Record the outcome per object. Failures never abort silently: the
//     command exits non-zero and writes a JSONL recovery record that can be
//     replayed with the same binary (updates are idempotent).
//  4. On full success, optionally remove the old version from the CRD's
//     status.storedVersions (--trim-stored-versions).
//
// The command uses the dynamic client and unstructured data, so it does not
// need either version's Go types to be linked into the caller.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"

	apiextensionsclientset "k8s.io/apiextensions-apiserver/pkg/client/clientset/clientset"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

const (
	group     = "migration.example.io"
	resource  = "tasks"
	crdName   = "tasks.migration.example.io"
	kind      = "Task"
	storedV1  = "v1"
	storedOld = "v1alpha1"
)

// record captures one object's migration outcome. The on-disk format is one
// JSON value per line (JSON Lines), so a partially written file from a killed
// run remains readable and replayable.
type record struct {
	Time         time.Time            `json:"time"`
	Name         string               `json:"name"`
	Namespace    string               `json:"namespace,omitempty"`
	FromVersion  string               `json:"fromVersion,omitempty"`
	ToVersion    string               `json:"toVersion"`
	Status       string               `json:"status"` // migrated | skipped | failed
	Reason       string               `json:"reason,omitempty"`
	HTTPStatus   int32                `json:"httpStatus,omitempty"`
	Error        string               `json:"error,omitempty"`
	ResourceName types.NamespacedName `json:"resourceName,omitempty"`
}

type options struct {
	kubeconfig         string
	masterURL          string
	namespace          string
	allNamespaces      bool
	recordFile         string
	failAfter          int
	dryRun             bool
	trimStoredVersions bool
	requestTimeout     time.Duration
}

func main() {
	o := options{}
	home, _ := os.UserHomeDir()
	defaultKubeconfig := filepath.Join(home, ".kube", "config")

	flag.StringVar(&o.kubeconfig, "kubeconfig", envStr("KUBECONFIG", defaultKubeconfig),
		"path to kubeconfig (ignored when running in-cluster)")
	flag.StringVar(&o.masterURL, "master", "", "API server URL override")
	flag.StringVar(&o.namespace, "namespace", "default", "namespace to migrate (ignored with --all-namespaces)")
	flag.BoolVar(&o.allNamespaces, "all-namespaces", true, "migrate objects in every namespace")
	flag.StringVar(&o.recordFile, "record-file", "migration-records.jsonl",
		"JSON Lines file the per-object recovery record is appended to")
	flag.IntVar(&o.failAfter, "fail-after", 0,
		"test hook: fail after N successful migrations (0 disables)")
	flag.BoolVar(&o.dryRun, "dry-run", false, "list and convert-read objects without updating")
	flag.BoolVar(&o.trimStoredVersions, "trim-stored-versions", true,
		"remove v1alpha1 from CRD status.storedVersions after a fully successful run")
	flag.DurationVar(&o.requestTimeout, "request-timeout", 30*time.Second, "per-object request timeout")
	flag.Parse()

	ctx := context.Background()

	cfg, err := loadRESTConfig(o)
	if err != nil {
		fatal("build REST config: %v", err)
	}
	cfg.Timeout = o.requestTimeout

	dyn, err := dynamic.NewForConfig(cfg)
	if err != nil {
		fatal("dynamic client: %v", err)
	}
	extClient, err := apiextensionsclientset.NewForConfig(cfg)
	if err != nil {
		fatal("apiextensions client: %v", err)
	}

	failed, migrated, err := runMigration(ctx, dyn, o)
	if err != nil {
		fatal("migration aborted: %v (recovery record: %s)", err, o.recordFile)
	}

	if failed == 0 && migrated > 0 && o.trimStoredVersions {
		if err := trimStoredVersions(ctx, extClient); err != nil {
			// Trimming is bookkeeping; object migration already succeeded.
			fmt.Fprintf(os.Stderr, "WARNING: objects migrated but storedVersions not trimmed: %v\n", err)
			os.Exit(2)
		}
		fmt.Println("CRD status.storedVersions trimmed to [v1]")
	}

	if failed > 0 {
		fmt.Fprintf(os.Stderr, "MIGRATION INCOMPLETE: %d object(s) failed; see %s and re-run\n", failed, o.recordFile)
		os.Exit(1)
	}
	fmt.Printf("migration complete: %d object(s) processed\n", migrated)
}

func runMigration(ctx context.Context, dyn dynamic.Interface, o options) (failed, migrated int, err error) {
	gvr := schema.GroupVersionResource{Group: group, Version: storedV1, Resource: resource}

	var list *unstructured.UnstructuredList
	if o.allNamespaces {
		list, err = dyn.Resource(gvr).List(ctx, metav1.ListOptions{})
	} else {
		list, err = dyn.Resource(gvr).Namespace(o.namespace).List(ctx, metav1.ListOptions{})
	}
	if err != nil {
		return 0, 0, fmt.Errorf("list tasks: %w", err)
	}

	rec, err := openRecordFile(o.recordFile)
	if err != nil {
		return 0, 0, err
	}
	defer rec.Close()

	fmt.Fprintf(os.Stderr, "found %d Task object(s) to consider\n", len(list.Items))

	for i := range list.Items {
		obj := &list.Items[i]
		nn := types.NamespacedName{Namespace: obj.GetNamespace(), Name: obj.GetName()}

		fromVersion := storedVersionHint(obj)
		alreadyV1 := obj.GetAPIVersion() == group+"/"+storedV1
		annoMigrated := obj.GetAnnotations()["migration.example.io/storage-version"] == storedV1

		r := record{
			Time:         time.Now().UTC(),
			Name:         nn.Name,
			Namespace:    nn.Namespace,
			FromVersion:  fromVersion,
			ToVersion:    storedV1,
			ResourceName: nn,
		}

		if alreadyV1 && annoMigrated {
			r.Status = "skipped"
			r.Reason = "already-stored-as-v1"
			writeRecord(rec, r)
			continue
		}

		if o.dryRun {
			r.Status = "skipped"
			r.Reason = "dry-run"
			writeRecord(rec, r)
			migrated++
			continue
		}

		// Stamp the migration annotation. The update is the actual storage
		// rewrite: server-side the bytes are the v1 representation the webhook
		// produced during the read.
		annos := obj.GetAnnotations()
		if annos == nil {
			annos = map[string]string{}
		}
		annos["migration.example.io/storage-version"] = storedV1
		annos["migration.example.io/migrated-at"] = time.Now().UTC().Format(time.RFC3339)
		obj.SetAnnotations(annos)

		// Keep the GVK intact: the dynamic client serializes the unstructured
		// object as-is and the API server rejects objects with empty
		// kind/apiVersion. Routing uses the GVR below, not the GVK.
		_, updateErr := dyn.Resource(gvr).Namespace(nn.Namespace).Update(ctx, obj, metav1.UpdateOptions{})
		if updateErr != nil {
			r.Status = "failed"
			r.Error = updateErr.Error()
			if apierrors.IsConflict(updateErr) {
				r.Reason = "conflict"
				r.HTTPStatus = 409
			} else {
				r.Reason = "update-error"
				if se, ok := updateErr.(apierrors.APIStatus); ok {
					r.HTTPStatus = se.Status().Code
				}
			}
			writeRecord(rec, r)
			failed++
			continue
		}

		r.Status = "migrated"
		writeRecord(rec, r)
		migrated++

		if o.failAfter > 0 && migrated >= o.failAfter {
			return failed, migrated, fmt.Errorf("injected failure after %d migration(s) (--fail-after); re-run to resume", o.failAfter)
		}
	}
	return failed, migrated, nil
}

// storedVersionHint reads the API version the object was served as. It is a
// hint for the recovery record; the authoritative signal is etcd bytes,
// which the API server does not expose.
func storedVersionHint(obj *unstructured.Unstructured) string {
	if obj.GetAPIVersion() == group+"/"+storedV1 {
		return storedOld // the webhook just converted it during the read
	}
	return obj.GetAPIVersion()
}

func trimStoredVersions(ctx context.Context, client apiextensionsclientset.Interface) error {
	crd, err := client.ApiextensionsV1().CustomResourceDefinitions().Get(ctx, crdName, metav1.GetOptions{})
	if err != nil {
		return fmt.Errorf("get CRD: %w", err)
	}
	kept := []string{}
	for _, v := range crd.Status.StoredVersions {
		if v != storedOld {
			kept = append(kept, v)
		}
	}
	if len(kept) == len(crd.Status.StoredVersions) {
		return nil
	}
	crd.Status.StoredVersions = kept
	_, err = client.ApiextensionsV1().CustomResourceDefinitions().UpdateStatus(ctx, crd, metav1.UpdateOptions{})
	if err != nil {
		return fmt.Errorf("update CRD storedVersions: %w", err)
	}
	return nil
}

func loadRESTConfig(o options) (*rest.Config, error) {
	if cfg, err := rest.InClusterConfig(); err == nil {
		return cfg, nil
	}
	rules := clientcmd.NewDefaultClientConfigLoadingRules()
	if o.kubeconfig != "" {
		rules.ExplicitPath = o.kubeconfig
	}
	overrides := &clientcmd.ConfigOverrides{}
	if o.masterURL != "" {
		overrides.ClusterInfo.Server = o.masterURL
	}
	return clientcmd.NewNonInteractiveDeferredLoadingClientConfig(rules, overrides).ClientConfig()
}

func openRecordFile(path string) (*os.File, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil && filepath.Dir(path) != "." {
		return nil, fmt.Errorf("create record directory: %w", err)
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("open record file %s: %w", path, err)
	}
	return f, nil
}

func writeRecord(f *os.File, r record) {
	b, _ := json.Marshal(r)
	_, _ = f.Write(append(b, '\n'))
}

func envStr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "storage-migrate: "+format+"\n", args...)
	os.Exit(1)
}
