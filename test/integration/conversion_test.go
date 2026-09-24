//go:build integration

// Integration tests against a real kube-apiserver + etcd provided by
// envtest, with the real conversion/admission webhooks running over TLS with
// a generated CA. Run via `make integration-test`.
package integration

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/example/crd-migration-demo/internal/conversion"
	"github.com/example/crd-migration-demo/internal/migrator"
	"github.com/example/crd-migration-demo/test/testenv"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic"
)

var (
	gvrV1    = schema.GroupVersionResource{Group: "timer.example.com", Version: "v1", Resource: "timers"}
	gvrAlpha = schema.GroupVersionResource{Group: "timer.example.com", Version: "v1alpha1", Resource: "timers"}
)

func newDynamic(t *testing.T, e *testenv.Env) dynamic.Interface {
	t.Helper()
	d, err := dynamic.NewForConfig(e.Cfg)
	if err != nil {
		t.Fatalf("dynamic client: %v", err)
	}
	return d
}

func mustCreate(t *testing.T, ctx context.Context, d dynamic.Interface, gvr schema.GroupVersionResource, obj map[string]any) *unstructured.Unstructured {
	t.Helper()
	u := &unstructured.Unstructured{Object: obj}
	got, err := d.Resource(gvr).Namespace(u.GetNamespace()).Create(ctx, u, metav1.CreateOptions{})
	if err != nil {
		t.Fatalf("create %s/%s: %v", gvr.Version, u.GetName(), err)
	}
	return got
}

func freshNS(t *testing.T, ctx context.Context, e *testenv.Env, name string) {
	t.Helper()
	ns := &unstructured.Unstructured{}
	ns.SetAPIVersion("v1")
	ns.SetKind("Namespace")
	ns.SetName(name)
	cfg := e.Cfg
	client, err := dynamic.NewForConfig(cfg)
	if err != nil {
		t.Fatal(err)
	}
	_ = client.Resource(schema.GroupVersionResource{Version: "v1", Resource: "namespaces"}).
		Delete(ctx, name, metav1.DeleteOptions{})
	if _, err := client.Resource(schema.GroupVersionResource{Version: "v1", Resource: "namespaces"}).
		Create(ctx, ns, metav1.CreateOptions{}); err != nil && !strings.Contains(err.Error(), "already exists") {
		t.Fatalf("create namespace %s: %v", name, err)
	}
}

// TestRoundTripThroughAPIServer creates a v1alpha1 object (old client),
// reads it back as v1, then re-reads as v1alpha1 and asserts field-level
// fidelity of durations + extra bag across the served versions.
func TestRoundTripThroughAPIServer(t *testing.T) {
	e := testenv.Start(t, "v1")
	ctx := context.Background()
	d := newDynamic(t, e)
	freshNS(t, ctx, e, "rt-ns")

	mustCreate(t, ctx, d, gvrAlpha, map[string]any{
		"apiVersion": "timer.example.com/v1alpha1",
		"kind":       "Timer",
		"metadata":   map[string]any{"name": "rt-obj", "namespace": "rt-ns"},
		"spec": map[string]any{
			"intervalSeconds": int64(30),
			"timeoutSeconds":  int64(5),
			"extra":           map[string]any{"region": "cn-north-1", "n": float64(7), "ok": true},
		},
	})

	// Read as v1: conversion webhook fires.
	v1obj, err := d.Resource(gvrV1).Namespace("rt-ns").Get(ctx, "rt-obj", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get as v1: %v", err)
	}
	interval, _, err := unstructured.NestedMap(v1obj.Object, "spec", "interval")
	if err != nil {
		t.Fatalf("v1 interval: %v", err)
	}
	if interval["seconds"] != int64(30) {
		t.Fatalf("v1 interval.seconds = %v (%T), want 30", interval["seconds"], interval["seconds"])
	}
	extra, _, err := unstructured.NestedMap(v1obj.Object, "spec", "extra")
	if err != nil {
		t.Fatalf("v1 extra missing: %v", err)
	}
	if extra["region"] != "cn-north-1" || extra["ok"] != true {
		t.Fatalf("v1 extra not preserved: %v", extra)
	}
	// priority is a v1-only field: object created without it must be
	// defaulted to Normal by CRD schema defaulting.
	prio, _, _ := unstructured.NestedString(v1obj.Object, "spec", "priority")
	if prio != "Normal" {
		t.Fatalf("defaulted priority = %q, want Normal", prio)
	}

	// Read back as v1alpha1: structured duration collapses to seconds.
	a1, err := d.Resource(gvrAlpha).Namespace("rt-ns").Get(ctx, "rt-obj", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get as v1alpha1: %v", err)
	}
	isec, _, err := unstructured.NestedInt64(a1.Object, "spec", "intervalSeconds")
	if err != nil {
		t.Fatalf("alpha intervalSeconds missing: %v (full=%v)", err, a1.Object)
	}
	if isec != 30 {
		t.Fatalf("alpha intervalSeconds = %d, want 30", isec)
	}
	alphaExtra, _, _ := unstructured.NestedMap(a1.Object, "spec", "extra")
	if alphaExtra["n"] != int64(7) {
		t.Fatalf("alpha extra numeric value not preserved: %v", alphaExtra)
	}
}

// TestDefaultedFields exercises both defaulting paths: v1 create without
// priority gets "Normal"; alpha create is upgraded and defaulted in storage.
func TestDefaultedFields(t *testing.T) {
	e := testenv.Start(t, "v1")
	ctx := context.Background()
	d := newDynamic(t, e)
	freshNS(t, ctx, e, "def-ns")

	// v1 create omitting priority -> mutating webhook + schema default.
	mustCreate(t, ctx, d, gvrV1, map[string]any{
		"apiVersion": "timer.example.com/v1",
		"kind":       "Timer",
		"metadata":   map[string]any{"name": "v1-no-prio", "namespace": "def-ns"},
		"spec":       map[string]any{"interval": map[string]any{"seconds": 60}},
	})
	got, err := d.Resource(gvrV1).Namespace("def-ns").Get(ctx, "v1-no-prio", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	p, _, _ := unstructured.NestedString(got.Object, "spec", "priority")
	if p != "Normal" {
		t.Fatalf("v1 create priority = %q, want Normal", p)
	}
	if _, found, _ := unstructured.NestedMap(got.Object, "spec", "timeout"); found {
		t.Fatal("omitted timeout should stay absent, not defaulted")
	}

	// v1alpha1 create: the stored v1 object gets schema-defaulted priority.
	mustCreate(t, ctx, d, gvrAlpha, map[string]any{
		"apiVersion": "timer.example.com/v1alpha1",
		"kind":       "Timer",
		"metadata":   map[string]any{"name": "alpha-no-prio", "namespace": "def-ns"},
		"spec":       map[string]any{"intervalSeconds": 1},
	})
	a1, err := d.Resource(gvrV1).Namespace("def-ns").Get(ctx, "alpha-no-prio", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get alpha-created as v1: %v", err)
	}
	p2, _, _ := unstructured.NestedString(a1.Object, "spec", "priority")
	if p2 != "Normal" {
		t.Fatalf("alpha-created priority in v1 storage = %q, want Normal", p2)
	}
}

// TestInvalidDurationsRejected verifies the validating webhook (and CRD
// schema) reject illegal durations at admission time.
func TestInvalidDurationsRejected(t *testing.T) {
	e := testenv.Start(t, "v1")
	ctx := context.Background()
	d := newDynamic(t, e)
	freshNS(t, ctx, e, "bad-ns")

	cases := []struct {
		name string
		spec map[string]any
		want string
	}{
		{"sign-mismatch", map[string]any{"interval": map[string]any{"seconds": 1, "nanos": -1}}, "same sign"},
		{"zero-interval", map[string]any{"interval": map[string]any{"seconds": 0}}, "strictly positive"},
		{"bad-priority", map[string]any{"interval": map[string]any{"seconds": 1}, "priority": "Urgent"}, "supported value"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			u := &unstructured.Unstructured{Object: map[string]any{
				"apiVersion": "timer.example.com/v1",
				"kind":       "Timer",
				"metadata":   map[string]any{"name": tc.name, "namespace": "bad-ns", "generateName": ""},
				"spec":       tc.spec,
			}}
			_, err := d.Resource(gvrV1).Namespace("bad-ns").Create(ctx, u, metav1.CreateOptions{})
			if err == nil {
				t.Fatalf("invalid object %s was accepted", tc.name)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error %q does not mention %q", err.Error(), tc.want)
			}
		})
	}

	// Schema-level guard: nanos out of range is rejected before/alongside
	// the webhook, and the alpha minimum also holds.
	u := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "timer.example.com/v1",
		"kind":       "Timer",
		"metadata":   map[string]any{"name": "nanos-range", "namespace": "bad-ns"},
		"spec":       map[string]any{"interval": map[string]any{"seconds": 1, "nanos": 1000000000}},
	}}
	if _, err := d.Resource(gvrV1).Namespace("bad-ns").Create(ctx, u, metav1.CreateOptions{}); err == nil {
		t.Fatal("nanos=1e9 must be rejected by schema")
	}
}

// TestOldClientUpdatePreservesV1Fields is the headline compatibility
// guarantee: an old v1alpha1-only client updating an object created by a v1
// client must not erase spec.priority.
func TestOldClientUpdatePreservesV1Fields(t *testing.T) {
	e := testenv.Start(t, "v1")
	ctx := context.Background()
	d := newDynamic(t, e)
	freshNS(t, ctx, e, "oc-ns")

	// New client creates with priority High + sub-second-free interval.
	mustCreate(t, ctx, d, gvrV1, map[string]any{
		"apiVersion": "timer.example.com/v1",
		"kind":       "Timer",
		"metadata":   map[string]any{"name": "shared", "namespace": "oc-ns"},
		"spec": map[string]any{
			"interval": map[string]any{"seconds": 10},
			"priority": "High",
			"extra":    map[string]any{"v1only": "kept"},
		},
	})

	// Old client: GET as v1alpha1, mutate a legacy field, PUT back.
	oldView, err := d.Resource(gvrAlpha).Namespace("oc-ns").Get(ctx, "shared", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("old client get: %v", err)
	}
	if ann := oldView.GetAnnotations()[conversion.V1PriorityAnnotation]; ann != "High" {
		t.Fatalf("priority annotation on old view = %q, want High", ann)
	}
	if err := unstructured.SetNestedField(oldView.Object, int64(20), "spec", "intervalSeconds"); err != nil {
		t.Fatal(err)
	}
	if _, err := d.Resource(gvrAlpha).Namespace("oc-ns").Update(ctx, oldView, metav1.UpdateOptions{}); err != nil {
		t.Fatalf("old client update: %v", err)
	}

	// New client reads v1 again: priority survives and the edit applied.
	newView, err := d.Resource(gvrV1).Namespace("oc-ns").Get(ctx, "shared", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("new client get: %v", err)
	}
	secs, _, _ := unstructured.NestedInt64(newView.Object, "spec", "interval", "seconds")
	if secs != 20 {
		t.Fatalf("old-client edit lost: seconds=%d", secs)
	}
	p, _, _ := unstructured.NestedString(newView.Object, "spec", "priority")
	if p != "High" {
		t.Fatalf("priority erased by old client: %q", p)
	}
	ex, _, _ := unstructured.NestedMap(newView.Object, "spec", "extra")
	if ex["v1only"] != "kept" {
		t.Fatalf("extra erased by old client: %v", ex)
	}
}

// TestUnknownFieldPolicy verifies the schema-declared policy explicitly:
// unknown top-level spec fields are pruned; unknown keys under spec.extra
// are preserved (x-kubernetes-preserve-unknown-fields).
func TestUnknownFieldPolicy(t *testing.T) {
	e := testenv.Start(t, "v1")
	ctx := context.Background()
	d := newDynamic(t, e)
	freshNS(t, ctx, e, "uf-ns")

	mustCreate(t, ctx, d, gvrV1, map[string]any{
		"apiVersion": "timer.example.com/v1",
		"kind":       "Timer",
		"metadata":   map[string]any{"name": "unknowns", "namespace": "uf-ns"},
		"spec": map[string]any{
			"interval":   map[string]any{"seconds": 1},
			"mysteryTop": "should-be-pruned",
			"extra":      map[string]any{"anything": map[string]any{"goes": []any{1, 2, 3}}},
		},
	})
	got, err := d.Resource(gvrV1).Namespace("uf-ns").Get(ctx, "unknowns", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	spec := got.Object["spec"].(map[string]any)
	if _, present := spec["mysteryTop"]; present {
		t.Fatalf("unknown spec field was NOT pruned: %v", spec["mysteryTop"])
	}
	extra := spec["extra"].(map[string]any)
	inner, ok := extra["anything"].(map[string]any)
	if !ok {
		t.Fatalf("extra contents lost: %v", extra)
	}
	arr, _ := inner["goes"].([]any)
	if len(arr) != 3 {
		t.Fatalf("preserved-unknown node altered: %v", inner)
	}
}

// TestSubsecondDowngradeFails proves unrepresentable v1 values fail loudly
// through the real apiserver when an old client reads them.
func TestSubsecondDowngradeFails(t *testing.T) {
	e := testenv.Start(t, "v1")
	ctx := context.Background()
	d := newDynamic(t, e)
	freshNS(t, ctx, e, "sub-ns")

	mustCreate(t, ctx, d, gvrV1, map[string]any{
		"apiVersion": "timer.example.com/v1",
		"kind":       "Timer",
		"metadata":   map[string]any{"name": "sub", "namespace": "sub-ns"},
		"spec":       map[string]any{"interval": map[string]any{"seconds": 0, "nanos": 250000000}},
	})

	_, err := d.Resource(gvrAlpha).Namespace("sub-ns").Get(ctx, "sub", metav1.GetOptions{})
	if err == nil {
		t.Fatal("expected downgrade of sub-second object to fail")
	}
	if !strings.Contains(err.Error(), "cannot be expressed in v1alpha1") {
		t.Fatalf("error should be explicit about representability, got: %v", err)
	}
}

// TestConversionTimeout exercises a slow webhook: an object carrying the
// inject-delay annotation makes the webhook block while converting between
// stored and served version. The apiserver only invokes the conversion
// webhook when the requested version differs from storage, so here storage
// is v1alpha1 and clients request v1. A client with a short deadline
// receives an error rather than hanging.
func TestConversionTimeout(t *testing.T) {
	e := testenv.Start(t, "v1alpha1")
	ctx := context.Background()
	d := newDynamic(t, e)
	freshNS(t, ctx, e, "slow-ns")

	before := e.ConversionH.ConversionsSucceeded.Load()
	mustCreate(t, ctx, d, gvrAlpha, map[string]any{
		"apiVersion": "timer.example.com/v1alpha1",
		"kind":       "Timer",
		"metadata": map[string]any{
			"name": "slow", "namespace": "slow-ns",
			"annotations": map[string]string{conversion.InjectDelayAnnotation: "3s"},
		},
		"spec": map[string]any{"intervalSeconds": 1},
	})

	// Patient client: storage v1alpha1 -> served v1 forces conversion, the
	// webhook blocks for 3s then succeeds.
	patient, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	obj, err := d.Resource(gvrV1).Namespace("slow-ns").Get(patient, "slow", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("patient client should succeed: %v", err)
	}
	if s, _, _ := unstructured.NestedInt64(obj.Object, "spec", "interval", "seconds"); s != 1 {
		t.Fatalf("unexpected object: %v", obj.Object["spec"])
	}
	if e.ConversionH.ConversionsSucceeded.Load() <= before {
		t.Fatal("expected the conversion webhook to be invoked at least once")
	}

	// Impatient client: 500ms deadline vs 3s webhook delay -> error near
	// the client deadline (the apiserver aborts the upstream call when the
	// client disconnects).
	short, cancel2 := context.WithTimeout(ctx, 500*time.Millisecond)
	defer cancel2()
	start := time.Now()
	_, err = d.Resource(gvrV1).Namespace("slow-ns").Get(short, "slow", metav1.GetOptions{})
	elapsed := time.Since(start)
	if err == nil {
		t.Fatal("impatient client unexpectedly succeeded")
	}
	if elapsed > 2*time.Second {
		t.Fatalf("client waited %s, expected deadline to abort near 500ms", elapsed)
	}
}

// TestStorageMigrationAndFailureRecord runs the real end-to-end migration:
// create objects while v1alpha1 is storage, switch storage to v1, run the
// migrator, assert storedVersions and per-object rewrite, then insert a
// sub-second object and run the DOWN migrator to produce a genuine failure
// record on disk.
func TestStorageMigrationAndFailureRecord(t *testing.T) {
	// Phase 1: cluster starts with v1alpha1 storage.
	e := testenv.Start(t, "v1alpha1")
	ctx := context.Background()
	d := newDynamic(t, e)
	freshNS(t, ctx, e, "mig-ns")

	for i, name := range []string{"m1", "m2", "m3"} {
		mustCreate(t, ctx, d, gvrAlpha, map[string]any{
			"apiVersion": "timer.example.com/v1alpha1",
			"kind":       "Timer",
			"metadata":   map[string]any{"name": name, "namespace": "mig-ns"},
			"spec":       map[string]any{"intervalSeconds": int64(10 + i)},
		})
	}

	crdBefore, err := e.APIExtClient.ApiextensionsV1().CustomResourceDefinitions().
		Get(ctx, "timers.timer.example.com", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if len(crdBefore.Status.StoredVersions) != 1 || crdBefore.Status.StoredVersions[0] != "v1alpha1" {
		t.Fatalf("storedVersions before = %v, want [v1alpha1]", crdBefore.Status.StoredVersions)
	}

	// Flip storage to v1 (objects still encoded v1alpha1 in etcd). The
	// apiserver immediately adds v1 to status.storedVersions on the CRD
	// spec update (standard behaviour); v1alpha1 stays there until every
	// object has been rewritten and the status is patched below.
	e.SetStorageVersion(t, "v1")

	// Run the migrator: rewrites via v1, forcing re-encoding.
	recordPath := filepath.Join(t.TempDir(), "migration-up.json")
	rec, err := migrator.Run(ctx, migrator.Options{
		ExtClient:  e.APIExtClient,
		Dynamic:    d,
		Direction:  "up",
		RecordPath: recordPath,
	})
	if err != nil {
		t.Fatalf("up migration: %v", err)
	}
	if rec.Migrated != 3 || rec.Failed != 0 {
		t.Fatalf("record totals = %+v", rec)
	}
	// After the storage flip both versions are advertised: the sweep just
	// ran rewrites everything as v1, but removing v1alpha1 is the explicit
	// status patch below once the operator verifies the record.
	if !containsString(rec.StorageBefore, "v1") {
		t.Fatalf("storageVersionsBefore = %v, want it to contain v1 after flip", rec.StorageBefore)
	}

	// Drop v1alpha1 from storedVersions now that nothing is stored in it.
	patchStoredVersions(t, e, []string{"v1"})
	crdAfter, err := e.APIExtClient.ApiextensionsV1().CustomResourceDefinitions().
		Get(ctx, "timers.timer.example.com", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if len(crdAfter.Status.StoredVersions) != 1 || crdAfter.Status.StoredVersions[0] != "v1" {
		t.Fatalf("storedVersions after = %v, want [v1]", crdAfter.Status.StoredVersions)
	}

	// Objects remain fully readable via the old API via conversion.
	a1, err := d.Resource(gvrAlpha).Namespace("mig-ns").Get(ctx, "m1", metav1.GetOptions{})
	if err != nil {
		t.Fatalf("read migrated object as v1alpha1: %v", err)
	}
	if s, _, _ := unstructured.NestedInt64(a1.Object, "spec", "intervalSeconds"); s != 10 {
		t.Fatalf("migrated object data changed: %d", s)
	}

	// Phase 2: genuine failure - sub-second object cannot downgrade.
	mustCreate(t, ctx, d, gvrV1, map[string]any{
		"apiVersion": "timer.example.com/v1",
		"kind":       "Timer",
		"metadata":   map[string]any{"name": "subsecond-blocker", "namespace": "mig-ns"},
		"spec":       map[string]any{"interval": map[string]any{"seconds": 0, "nanos": 500000000}},
	})
	failPath := filepath.Join(t.TempDir(), "migration-down-failed.json")
	rec2, err := migrator.Run(ctx, migrator.Options{
		ExtClient:  e.APIExtClient,
		Dynamic:    d,
		Direction:  "down",
		RecordPath: failPath,
	})
	if err == nil {
		t.Fatal("down migration with sub-second object must fail")
	}
	if rec2 == nil || rec2.Failed == 0 {
		t.Fatalf("expected failure record with Failed>0, got %+v", rec2)
	}
	raw, readErr := os.ReadFile(failPath)
	if readErr != nil {
		t.Fatalf("failure record not written: %v", readErr)
	}
	var onDisk map[string]any
	if err := json.Unmarshal(raw, &onDisk); err != nil {
		t.Fatalf("record is not valid JSON: %v", err)
	}
	if onDisk["failed"].(float64) == 0 {
		t.Fatalf("recorded failure count is 0: %s", raw)
	}
	if !strings.Contains(fmt.Sprint(onDisk), "cannot be expressed in v1alpha1") &&
		!strings.Contains(fmt.Sprint(onDisk), "convert") &&
		!strings.Contains(fmt.Sprint(onDisk), "conversion") {
		t.Fatalf("record should explain the conversion failure: %s", raw)
	}
	t.Logf("failure record written to %s (%d bytes)", failPath, len(raw))

	// Publish a deterministic copy for the runbook/acceptance evidence.
	if dir := os.Getenv("FIXTURE_OUT_DIR"); dir != "" {
		if err := os.MkdirAll(dir, 0o755); err == nil {
			_ = os.WriteFile(filepath.Join(dir, "migration-down-failed.json"), raw, 0o644)
		}
	}
}

func containsString(haystack []string, needle string) bool {
	for _, s := range haystack {
		if s == needle {
			return true
		}
	}
	return false
}

func patchStoredVersions(t *testing.T, e *testenv.Env, versions []string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	crd, err := e.APIExtClient.ApiextensionsV1().CustomResourceDefinitions().
		Get(ctx, "timers.timer.example.com", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	crd.Status.StoredVersions = versions
	if _, err := e.APIExtClient.ApiextensionsV1().CustomResourceDefinitions().
		UpdateStatus(ctx, crd, metav1.UpdateOptions{}); err != nil {
		t.Fatalf("updating storedVersions: %v", err)
	}
}
