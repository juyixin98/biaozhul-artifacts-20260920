package integration

import (
	"bufio"
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	apiextensionsclient "k8s.io/apiextensions-apiserver/pkg/client/clientset/clientset"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

var storageMigrateBin string

// TestStorageMigrateCLI exercises the real storage migration binary as a
// subprocess against the envtest cluster:
//
//	seed v1alpha1 objects -> flip CRD storage version to v1alpha1 for seeding
//	not required here (objects are stored as v1 on create and converted on
//	read, so the migration still performs real GET/UPDATE rewrites) -> run CLI
//	-> JSONL records written -> re-run is idempotent.
//
// Note on fixture shape: in a freshly installed CRD storage=v1 world the
// objects are already v1 bytes, so the CLI's migration annotation marks
// them; a pre-rollout cluster stores v1alpha1 bytes and the same code path
// performs the actual conversion. The failure/recovery semantics are tested
// deterministically with --fail-after below.
func TestStorageMigrateCLI(t *testing.T) {
	ctx := context.Background()

	// Seed two objects through the legacy API. Both exist before the CLI
	// runs; the migration performs a real GET (server-side conversion) +
	// UPDATE (storage rewrite) for each.
	for _, name := range []string{"mig-a", "mig-b"} {
		u := &unstructured.Unstructured{}
		u.SetGroupVersionKind(schema.GroupVersionKind{
			Group: "migration.example.io", Version: "v1alpha1", Kind: "Task",
		})
		u.SetName(name)
		u.SetNamespace("itest")
		require.NoError(t, unstructured.SetNestedField(u.Object, name, "spec", "payload"))
		_, err := dynamicClient(t).Resource(gvrV1Alpha1).Namespace("itest").
			Create(ctx, u, metav1.CreateOptions{})
		require.NoError(t, err)
	}

	records := filepath.Join(t.TempDir(), "records.jsonl")

	// First run: migrate everything, fail-after injects a crash AFTER the
	// first object so the recovery record captures a partial run.
	out1, err := runMigrate(ctx, t, records, 1, false)
	require.Error(t, err, "expected non-zero exit from injected failure\n%s", out1)
	require.FileExists(t, records)

	recs := readRecords(t, records)
	require.GreaterOrEqual(t, len(recs), 1)
	var sawMigrated bool
	for _, r := range recs {
		if r["status"] == "migrated" {
			sawMigrated = true
		}
	}
	assert.True(t, sawMigrated, "at least one object should be migrated before failure")

	// Re-run without injection: resumes, migrates the rest. Idempotent on the
	// already-migrated object.
	out2, err := runMigrate(ctx, t, records, 0, false)
	require.NoError(t, err, "second run should succeed and resume\n%s", out2)

	recs2 := readRecords(t, records)
	var migrated, skipped int
	for _, r := range recs2 {
		switch r["status"] {
		case "migrated":
			migrated++
		case "skipped":
			skipped++
		}
	}
	require.GreaterOrEqual(t, migrated, 2, "both seeded objects must be migrated")
	assert.GreaterOrEqual(t, skipped, 1, "the object migrated in run 1 must be idempotently skipped")

	// storedVersions is trimmed to v1 on a fully successful run.
	cs, err := apiextensionsclient.NewForConfig(restCfg)
	require.NoError(t, err)
	crd, err := cs.ApiextensionsV1().CustomResourceDefinitions().Get(ctx, "tasks.migration.example.io", metav1.GetOptions{})
	require.NoError(t, err)
	assert.ElementsMatch(t, []string{"v1"}, crd.Status.StoredVersions)

	// Every migrated object carries the storage-version annotation.
	list, err := dynamicClient(t).Resource(schema.GroupVersionResource{
		Group: "migration.example.io", Version: "v1", Resource: "tasks",
	}).Namespace("itest").List(ctx, metav1.ListOptions{})
	require.NoError(t, err)
	var foundSeeded int
	for _, item := range list.Items {
		if strings.HasPrefix(item.GetName(), "mig-") {
			foundSeeded++
			assert.Equal(t, "v1", item.GetAnnotations()["migration.example.io/storage-version"])
			_, err := time.Parse(time.RFC3339, item.GetAnnotations()["migration.example.io/migrated-at"])
			assert.NoError(t, err)
		}
	}
	assert.Equal(t, 2, foundSeeded)
}

// TestStorageMigrateDryRun verifies --dry-run writes records but never calls
// update (objects have no migration annotation afterwards).
func TestStorageMigrateDryRun(t *testing.T) {
	ctx := context.Background()
	u := &unstructured.Unstructured{}
	u.SetGroupVersionKind(schema.GroupVersionKind{
		Group: "migration.example.io", Version: "v1alpha1", Kind: "Task",
	})
	u.SetName("mig-dry")
	u.SetNamespace("itest")
	require.NoError(t, unstructured.SetNestedField(u.Object, "dry", "spec", "payload"))
	_, err := dynamicClient(t).Resource(gvrV1Alpha1).Namespace("itest").Create(ctx, u, metav1.CreateOptions{})
	require.NoError(t, err)

	records := filepath.Join(t.TempDir(), "dry.jsonl")
	out, err := runMigrate(ctx, t, records, 0, true)
	require.NoError(t, err, out)

	got, err := dynamicClient(t).Resource(gvrV1).Namespace("itest").Get(ctx, "mig-dry", metav1.GetOptions{})
	require.NoError(t, err)
	assert.NotContains(t, got.GetAnnotations(), "migration.example.io/storage-version")

	recs := readRecords(t, records)
	var drySkipped int
	for _, r := range recs {
		if r["reason"] == "dry-run" {
			drySkipped++
		}
	}
	assert.GreaterOrEqual(t, drySkipped, 1)
}

func runMigrate(ctx context.Context, t *testing.T, records string, failAfter int, dryRun bool) ([]byte, error) {
	t.Helper()
	if storageMigrateBin == "" {
		t.Skip("storage-migrate binary not built")
	}
	args := []string{
		"--kubeconfig", kubeconfigPath,
		"--namespace", "itest",
		"--all-namespaces=false",
		"--record-file", records,
		"--trim-stored-versions=true",
		"--request-timeout", "10s",
	}
	if failAfter > 0 {
		args = append(args, "--fail-after", strconv.Itoa(failAfter))
	}
	if dryRun {
		args = append(args, "--dry-run")
	}
	cmd := exec.CommandContext(ctx, storageMigrateBin, args...)
	return cmd.CombinedOutput()
}

func readRecords(t *testing.T, path string) []map[string]any {
	t.Helper()
	f, err := os.Open(path)
	require.NoError(t, err)
	defer f.Close()
	var out []map[string]any
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		var r map[string]any
		require.NoError(t, json.Unmarshal([]byte(line), &r))
		out = append(out, r)
	}
	require.NoError(t, sc.Err())
	return out
}
