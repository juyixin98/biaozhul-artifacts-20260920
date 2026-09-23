package controller

import (
	"context"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	distv1alpha1 "github.com/example/config-distributor/api/v1alpha1"
)

const (
	testUID     = types.UID("uid-cr-1")
	testUIDReb  = types.UID("uid-cr-2")
	testUIDElse = types.UID("uid-foreign")
)

func newScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(s); err != nil {
		t.Fatal(err)
	}
	if err := distv1alpha1.AddToScheme(s); err != nil {
		t.Fatal(err)
	}
	return s
}

func testCR(name string, uid types.UID, selector map[string]string) *distv1alpha1.ConfigDistribution {
	return &distv1alpha1.ConfigDistribution{
		ObjectMeta: metav1.ObjectMeta{
			Name:       name,
			UID:        uid,
			Generation: 1,
		},
		Spec: distv1alpha1.ConfigDistributionSpec{
			Config: map[string]string{"app.conf": "mode=fast", "flags": "a=1"},
			NamespaceSelector: metav1.LabelSelector{
				MatchLabels: selector,
			},
		},
	}
}

func testNS(name string, labels map[string]string) *corev1.Namespace {
	return &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name, Labels: labels}}
}

func newReconciler(t *testing.T, objs ...client.Object) (*ConfigDistributionReconciler, client.Client) {
	t.Helper()
	s := newScheme(t)
	c := fake.NewClientBuilder().
		WithScheme(s).
		WithObjects(objs...).
		WithStatusSubresource(&distv1alpha1.ConfigDistribution{}).
		Build()
	return &ConfigDistributionReconciler{Client: c, Scheme: s}, c
}

func doReconcile(t *testing.T, r *ConfigDistributionReconciler, name string) ctrl.Result {
	t.Helper()
	res, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: name},
	})
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	return res
}

func childNames(t *testing.T, c client.Client) []string {
	t.Helper()
	var cms corev1.ConfigMapList
	if err := c.List(context.Background(), &cms, client.MatchingLabels{LabelManagedBy: ManagedByValue}); err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, cm := range cms.Items {
		out = append(out, cm.Namespace+"/"+cm.Name)
	}
	return out
}

func getChild(t *testing.T, c client.Client, ns, name string) *corev1.ConfigMap {
	t.Helper()
	var cm corev1.ConfigMap
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: name}, &cm); err != nil {
		t.Fatalf("get child %s/%s: %v", ns, name, err)
	}
	return &cm
}

func getCR(t *testing.T, c client.Client, name string) *distv1alpha1.ConfigDistribution {
	t.Helper()
	var cr distv1alpha1.ConfigDistribution
	if err := c.Get(context.Background(), types.NamespacedName{Name: name}, &cr); err != nil {
		t.Fatalf("get cr: %v", err)
	}
	return &cr
}

func targetPhase(cr *distv1alpha1.ConfigDistribution, ns string) distv1alpha1.TargetPhase {
	for _, tg := range cr.Status.Targets {
		if tg.Namespace == ns {
			return tg.Phase
		}
	}
	return ""
}

// TestDistributeToSelectedNamespaces verifies basic fan-out and per-target status.
func TestDistributeToSelectedNamespaces(t *testing.T) {
	cr := testCR("main", testUID, map[string]string{"team": "a"})
	r, c := newReconciler(t, cr,
		testNS("ns-1", map[string]string{"team": "a"}),
		testNS("ns-2", map[string]string{"team": "a"}),
		testNS("ns-3", map[string]string{"team": "b"}),
	)
	doReconcile(t, r, "main")

	digest := ConfigDigest(cr.Spec.Config)
	want := ChildName("main", digest)
	for _, ns := range []string{"ns-1", "ns-2"} {
		cm := getChild(t, c, ns, want)
		if cm.Data["app.conf"] != "mode=fast" {
			t.Errorf("child %s data mismatch: %v", ns, cm.Data)
		}
		if cm.Labels[LabelOwnerUID] != string(testUID) {
			t.Errorf("child %s missing owner uid label", ns)
		}
		if !hasOwnerRef(cm, testUID) {
			t.Errorf("child %s missing owner reference", ns)
		}
	}
	if got := childNames(t, c); len(got) != 2 {
		t.Errorf("expected 2 children, got %v", got)
	}

	got := getCR(t, c, "main")
	if got.Status.ConfigDigest != digest {
		t.Errorf("status digest = %q, want %q", got.Status.ConfigDigest, digest)
	}
	for _, ns := range []string{"ns-1", "ns-2"} {
		if p := targetPhase(got, ns); p != distv1alpha1.TargetReady {
			t.Errorf("target %s phase = %q, want Ready", ns, p)
		}
	}
	if targetPhase(got, "ns-3") != "" {
		t.Errorf("ns-3 must not appear in status targets")
	}
}

// TestDuplicateEventsAndResyncAreNoOps: repeated reconciles (duplicate watch
// events, full resync) must not create duplicates nor rewrite existing children.
func TestDuplicateEventsAndResyncAreNoOps(t *testing.T) {
	cr := testCR("main", testUID, map[string]string{"team": "a"})
	r, c := newReconciler(t, cr, testNS("ns-1", map[string]string{"team": "a"}))

	doReconcile(t, r, "main")
	digest := ConfigDigest(cr.Spec.Config)
	before := getChild(t, c, "ns-1", ChildName("main", digest)).DeepCopy()

	// Duplicate events + simulated full resync: reconcile several more times.
	for i := 0; i < 5; i++ {
		doReconcile(t, r, "main")
	}
	if got := childNames(t, c); len(got) != 1 {
		t.Fatalf("expected exactly 1 child after duplicate events, got %v", got)
	}
	after := getChild(t, c, "ns-1", ChildName("main", digest))
	if after.ResourceVersion != before.ResourceVersion {
		t.Errorf("child was rewritten on no-op reconcile (rv %s -> %s)",
			before.ResourceVersion, after.ResourceVersion)
	}
	if after.UID != before.UID {
		t.Errorf("child was recreated (uid changed)")
	}
}

// TestSelectorShrinkDeletesOnlyOwned: shrinking the selector removes children
// in dropped namespaces, but a same-name foreign object (different owner UID)
// must survive.
func TestSelectorShrinkDeletesOnlyOwned(t *testing.T) {
	cr := testCR("main", testUID, map[string]string{"team": "a"})
	r, c := newReconciler(t, cr,
		testNS("keep", map[string]string{"team": "a"}),
		testNS("drop", map[string]string{"team": "a"}),
	)
	doReconcile(t, r, "main")
	digest := ConfigDigest(cr.Spec.Config)
	name := ChildName("main", digest)
	if got := childNames(t, c); len(got) != 2 {
		t.Fatalf("setup: expected 2 children, got %v", got)
	}

	// A foreign object with the SAME deterministic name in the dropped
	// namespace, owned by a different UID (e.g. left by another controller
	// generation or planted by a user). It must not be deleted.
	foreign := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "drop",
			Name:      name,
			Labels: map[string]string{
				LabelManagedBy: ManagedByValue,
				LabelOwnerUID:  string(testUIDElse),
			},
		},
		Data: map[string]string{"app.conf": "mode=foreign"},
	}
	// Replace our owned child in "drop" with the foreign one to simulate the
	// worst case: identical name, different owner.
	owned := getChild(t, c, "drop", name)
	if err := c.Delete(context.Background(), owned); err != nil {
		t.Fatal(err)
	}
	if err := c.Create(context.Background(), foreign); err != nil {
		t.Fatal(err)
	}

	// Shrink the selector: "drop" no longer matches.
	var dropNS corev1.Namespace
	if err := c.Get(context.Background(), types.NamespacedName{Name: "drop"}, &dropNS); err != nil {
		t.Fatal(err)
	}
	dropNS.Labels = map[string]string{"team": "b"}
	if err := c.Update(context.Background(), &dropNS); err != nil {
		t.Fatal(err)
	}

	doReconcile(t, r, "main")

	// Foreign object in dropped namespace must survive.
	var survived corev1.ConfigMap
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: "drop", Name: name}, &survived); err != nil {
		t.Fatalf("foreign same-name object was wrongly deleted: %v", err)
	}
	if survived.Data["app.conf"] != "mode=foreign" {
		t.Errorf("foreign object was modified: %v", survived.Data)
	}
	// Kept namespace still has its child.
	getChild(t, c, "keep", name)
	// Status only mentions the kept namespace.
	got := getCR(t, c, "main")
	if len(got.Status.Targets) != 1 || got.Status.Targets[0].Namespace != "keep" {
		t.Errorf("unexpected targets after shrink: %+v", got.Status.Targets)
	}
}

// TestOwnerRebuildAdoptsOrphans: the CR is deleted (finalizer force-removed,
// e.g. after a crash) leaving children behind; a new CR with the same name
// but a new UID must adopt the orphans, never delete them.
func TestOwnerRebuildAdoptsOrphans(t *testing.T) {
	old := testCR("main", testUID, map[string]string{"team": "a"})
	r, c := newReconciler(t, old, testNS("ns-1", map[string]string{"team": "a"}))
	doReconcile(t, r, "main")
	digest := ConfigDigest(old.Spec.Config)
	name := ChildName("main", digest)
	orphanUID := getChild(t, c, "ns-1", name).UID

	// Simulate force-delete of the CR while children remain (finalizer
	// removed out-of-band, controller down).
	var cur distv1alpha1.ConfigDistribution
	if err := c.Get(context.Background(), types.NamespacedName{Name: "main"}, &cur); err != nil {
		t.Fatal(err)
	}
	cur.Finalizers = nil
	if err := c.Update(context.Background(), &cur); err != nil {
		t.Fatal(err)
	}
	if err := c.Delete(context.Background(), &cur); err != nil {
		t.Fatal(err)
	}

	// Rebuild: same name, same spec, NEW uid.
	rebuilt := testCR("main", testUIDReb, map[string]string{"team": "a"})
	if err := c.Create(context.Background(), rebuilt); err != nil {
		t.Fatal(err)
	}
	doReconcile(t, r, "main")

	cm := getChild(t, c, "ns-1", name)
	if cm.UID != orphanUID {
		t.Errorf("orphan was deleted and recreated (uid %q -> %q); want adoption", orphanUID, cm.UID)
	}
	if cm.Labels[LabelOwnerUID] != string(testUIDReb) {
		t.Errorf("orphan not adopted: owner-uid label = %q", cm.Labels[LabelOwnerUID])
	}
	if !hasOwnerRef(cm, testUIDReb) {
		t.Errorf("orphan missing new owner reference")
	}
	if p := targetPhase(getCR(t, c, "main"), "ns-1"); p != distv1alpha1.TargetReady {
		t.Errorf("phase = %q, want Ready", p)
	}
}

// TestNameConflictNeverTouchesForeignObject: a foreign ConfigMap squatting on
// the deterministic child name is reported as Conflict and left untouched.
func TestNameConflictNeverTouchesForeignObject(t *testing.T) {
	cr := testCR("main", testUID, map[string]string{"team": "a"})
	digest := ConfigDigest(cr.Spec.Config)
	name := ChildName("main", digest)
	foreign := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "ns-1",
			Name:      name,
			Labels:    map[string]string{"app": "someone-else"},
		},
		Data: map[string]string{"app.conf": "mode=foreign"},
	}
	r, c := newReconciler(t, cr, testNS("ns-1", map[string]string{"team": "a"}), foreign)

	res := doReconcile(t, r, "main")
	if res.RequeueAfter == 0 {
		t.Errorf("conflict should requeue, got %+v", res)
	}
	got := getCR(t, c, "main")
	if p := targetPhase(got, "ns-1"); p != distv1alpha1.TargetConflict {
		t.Fatalf("phase = %q, want Conflict", p)
	}
	cm := getChild(t, c, "ns-1", name)
	if cm.Data["app.conf"] != "mode=foreign" {
		t.Errorf("foreign object modified: %v", cm.Data)
	}
	if hasOwnerRef(cm, testUID) {
		t.Errorf("foreign object claimed by our owner reference")
	}
}

// TestPartialFailureDoesNotRollBackSuccesses: one conflicting target fails
// while others succeed; retries keep the successful targets Ready and
// untouched, and eventually converge once the conflict is resolved.
func TestPartialFailureDoesNotRollBackSuccesses(t *testing.T) {
	cr := testCR("main", testUID, map[string]string{"team": "a"})
	digest := ConfigDigest(cr.Spec.Config)
	name := ChildName("main", digest)
	foreign := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Namespace: "bad", Name: name},
		Data:       map[string]string{"app.conf": "mode=foreign"},
	}
	r, c := newReconciler(t, cr,
		testNS("good-1", map[string]string{"team": "a"}),
		testNS("good-2", map[string]string{"team": "a"}),
		testNS("bad", map[string]string{"team": "a"}),
		foreign,
	)

	doReconcile(t, r, "main")
	got := getCR(t, c, "main")
	for _, ns := range []string{"good-1", "good-2"} {
		if p := targetPhase(got, ns); p != distv1alpha1.TargetReady {
			t.Errorf("target %s phase = %q, want Ready despite sibling failure", ns, p)
		}
	}
	if p := targetPhase(got, "bad"); p != distv1alpha1.TargetConflict {
		t.Errorf("target bad phase = %q, want Conflict", p)
	}
	rv1 := getChild(t, c, "good-1", name).ResourceVersion

	// Retry (backoff elapsed): successes must not be rewritten or rolled back.
	doReconcile(t, r, "main")
	if rv2 := getChild(t, c, "good-1", name).ResourceVersion; rv2 != rv1 {
		t.Errorf("successful target rewritten during retry of failed sibling")
	}
	got = getCR(t, c, "main")
	if p := targetPhase(got, "good-1"); p != distv1alpha1.TargetReady {
		t.Errorf("good-1 rolled back to %q", p)
	}

	// Operator removes the foreign object; next retry converges fully.
	if err := c.Delete(context.Background(), foreign); err != nil {
		t.Fatal(err)
	}
	doReconcile(t, r, "main")
	got = getCR(t, c, "main")
	for _, ns := range []string{"good-1", "good-2", "bad"} {
		if p := targetPhase(got, ns); p != distv1alpha1.TargetReady {
			t.Errorf("after conflict resolution target %s phase = %q", ns, p)
		}
	}
	if cond := got.Status.Conditions; len(cond) == 0 || cond[0].Status != metav1.ConditionTrue {
		t.Errorf("Ready condition not true after convergence: %+v", cond)
	}
}

// TestOutOfOrderEvents: level-based reconcile must converge regardless of
// event order — child deleted out-of-band, namespace relabeled, CR unchanged.
func TestOutOfOrderEvents(t *testing.T) {
	cr := testCR("main", testUID, map[string]string{"team": "a"})
	r, c := newReconciler(t, cr,
		testNS("ns-1", map[string]string{"team": "a"}),
		testNS("ns-2", map[string]string{"team": "a"}),
	)
	digest := ConfigDigest(cr.Spec.Config)
	name := ChildName("main", digest)

	// "Events" arrive in a scrambled order: delete before create is observed,
	// relabel before initial sync, etc. Each is just a reconcile.
	doReconcile(t, r, "main")                                  // create event
	doReconcile(t, r, "main")                                  // duplicate create event
	cm := getChild(t, c, "ns-1", name)                         //
	if err := c.Delete(context.Background(), cm); err != nil { // out-of-band delete
		t.Fatal(err)
	}
	doReconcile(t, r, "main") // delete event for ns-1 child -> recreate
	getChild(t, c, "ns-1", name)

	// ns-2 relabeled away, then CR update event arrives "late" (after the
	// namespace event): either order converges to the same state.
	var ns2 corev1.Namespace
	if err := c.Get(context.Background(), types.NamespacedName{Name: "ns-2"}, &ns2); err != nil {
		t.Fatal(err)
	}
	ns2.Labels = map[string]string{"team": "b"}
	if err := c.Update(context.Background(), &ns2); err != nil {
		t.Fatal(err)
	}
	doReconcile(t, r, "main") // namespace event
	doReconcile(t, r, "main") // late duplicate

	if got := childNames(t, c); len(got) != 1 || got[0] != "ns-1/"+name {
		t.Fatalf("expected only ns-1 child, got %v", got)
	}
	got := getCR(t, c, "main")
	if len(got.Status.Targets) != 1 || got.Status.Targets[0].Namespace != "ns-1" {
		t.Errorf("unexpected targets: %+v", got.Status.Targets)
	}
}

// TestProcessRestartIsIdempotent: a brand-new reconciler instance (process
// restarted, caches cold) over the same cluster state must be a pure no-op.
func TestProcessRestartIsIdempotent(t *testing.T) {
	cr := testCR("main", testUID, map[string]string{"team": "a"})
	r1, c := newReconciler(t, cr, testNS("ns-1", map[string]string{"team": "a"}))
	doReconcile(t, r1, "main")
	digest := ConfigDigest(cr.Spec.Config)
	name := ChildName("main", digest)
	before := getChild(t, c, "ns-1", name).DeepCopy()

	// "Restart": new reconciler instance against the same cluster.
	r2 := &ConfigDistributionReconciler{Client: c, Scheme: r1.Scheme}
	doReconcile(t, r2, "main")

	after := getChild(t, c, "ns-1", name)
	if after.UID != before.UID || after.ResourceVersion != before.ResourceVersion {
		t.Errorf("restart rewrote child: uid %q->%q rv %q->%q",
			before.UID, after.UID, before.ResourceVersion, after.ResourceVersion)
	}
	if got := childNames(t, c); len(got) != 1 {
		t.Errorf("restart created duplicates: %v", got)
	}
}

// TestDeleteRemovesOnlyOwnedChildren: CR deletion (finalizer) removes owned
// children across namespaces and leaves foreign objects alone.
func TestDeleteRemovesOnlyOwnedChildren(t *testing.T) {
	cr := testCR("main", testUID, map[string]string{"team": "a"})
	r, c := newReconciler(t, cr,
		testNS("ns-1", map[string]string{"team": "a"}),
		testNS("ns-2", map[string]string{"team": "a"}),
	)
	doReconcile(t, r, "main")

	// Foreign object squatting in ns-2 with a different name.
	foreign := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Namespace: "ns-2", Name: "foreign"},
		Data:       map[string]string{"x": "y"},
	}
	if err := c.Create(context.Background(), foreign); err != nil {
		t.Fatal(err)
	}

	// Delete the CR: finalizer keeps it around, cleanup runs, finalizer drops.
	cur := getCR(t, c, "main")
	if err := c.Delete(context.Background(), cur); err != nil {
		t.Fatal(err)
	}
	doReconcile(t, r, "main")

	if got := childNames(t, c); len(got) != 0 {
		t.Errorf("owned children remain after delete: %v", got)
	}
	var survived corev1.ConfigMap
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: "ns-2", Name: "foreign"}, &survived); err != nil {
		t.Errorf("foreign object deleted: %v", err)
	}
	var gone distv1alpha1.ConfigDistribution
	if err := c.Get(context.Background(), types.NamespacedName{Name: "main"}, &gone); err == nil {
		t.Errorf("CR still present (finalizer not removed)")
	}
}

// TestDigestStability: digest is order-independent and name-safe.
func TestDigestStability(t *testing.T) {
	a := ConfigDigest(map[string]string{"x": "1", "y": "2", "z": "3"})
	b := ConfigDigest(map[string]string{"z": "3", "x": "1", "y": "2"})
	if a != b {
		t.Errorf("digest not order-independent: %q vs %q", a, b)
	}
	c := ConfigDigest(map[string]string{"x": "1", "y": "2", "z": "4"})
	if a == c {
		t.Errorf("digest insensitive to value change")
	}
	if n := ChildName("main", a); len(n) > 63 {
		t.Errorf("child name too long: %q", n)
	}
}
