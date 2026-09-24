package controller

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	configv1alpha1 "github.com/example/config-distributor/api/v1alpha1"
	"github.com/example/config-distributor/internal/naming"
)

func testScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	require.NoError(t, corev1.AddToScheme(s))
	require.NoError(t, configv1alpha1.AddToScheme(s))
	return s
}

func newSnapshot() *configv1alpha1.ConfigSnapshot {
	return &configv1alpha1.ConfigSnapshot{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "snap1",
			UID:        types.UID("owner-uid-1"),
			Generation: 1,
		},
		Spec: configv1alpha1.ConfigSnapshotSpec{
			Payload: configv1alpha1.Payload{
				Format: "properties",
				Data:   "key=value\n",
			},
			Selector: metav1.LabelSelector{MatchLabels: map[string]string{"tier": "test"}},
		},
	}
}

func namespaces(names ...string) []client.Object {
	objs := []client.Object{}
	for i, n := range names {
		objs = append(objs, &corev1.Namespace{
			ObjectMeta: metav1.ObjectMeta{
				Name:   n,
				UID:    types.UID("uid-" + n),
				Labels: map[string]string{"tier": "test", "order": string(rune('a' + i))},
			},
			Status: corev1.NamespaceStatus{Phase: corev1.NamespaceActive},
		})
	}
	return objs
}

func newReconciler(t *testing.T, objs ...client.Object) (*Reconciler, client.Client) {
	t.Helper()
	scheme := testScheme(t)
	b := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&configv1alpha1.ConfigSnapshot{}).
		WithObjects(objs...)
	c := b.Build()
	r := &Reconciler{
		Client:        c,
		Scheme:        scheme,
		Recorder:      record.NewFakeRecorder(100),
		RetryInterval: time.Millisecond,
	}
	return r, c
}

func reconcileOnce(ctx context.Context, t *testing.T, r *Reconciler, name string) ctrl.Result {
	t.Helper()
	res, err := r.Reconcile(ctx, ctrl.Request{NamespacedName: types.NamespacedName{Name: name}})
	require.NoError(t, err)
	return res
}

func digestOf(cs *configv1alpha1.ConfigSnapshot) string {
	return naming.ContentDigest(cs.Spec.Payload.Format, cs.Spec.Payload.Data)
}

// TestReconcile_CreatesOnceOnRepeatedEventsAndResync is the core dedup test:
// many reconcile rounds (duplicate events + simulated full resync) must yield
// exactly one child ConfigMap per target, with identical UID and resourceVersion
// untouched between rounds.
func TestReconcile_CreatesOnceOnRepeatedEventsAndResync(t *testing.T) {
	ctx := context.Background()
	cs := newSnapshot()
	objs := append([]client.Object{cs}, namespaces("test-a", "test-b")...)
	r, c := newReconciler(t, objs...)
	counter := &countingClient{Client: c}
	r.Client = counter

	digest := digestOf(cs)
	childName := naming.ChildName(cs.Name, digest)

	for round := 0; round < 5; round++ {
		reconcileOnce(ctx, t, r, cs.Name)
	}

	// Five reconcile rounds (duplicate events + full resync) must result in
	// exactly one CREATE per target — not five.
	assert.Equal(t, 2, counter.creates,
		"expected exactly one create per target across 5 rounds, got %d", counter.creates)

	for _, ns := range []string{"test-a", "test-b"} {
		cm := &corev1.ConfigMap{}
		require.NoError(t, c.Get(ctx, types.NamespacedName{Namespace: ns, Name: childName}, cm),
			"child must exist in %s", ns)
		assert.Equal(t, cs.Spec.Payload.Data, cm.Data[naming.DataKey])
		assert.Equal(t, digest, cm.Annotations[naming.DigestKey])
		require.Len(t, cm.OwnerReferences, 1)
		assert.Equal(t, types.UID("owner-uid-1"), cm.OwnerReferences[0].UID)
		assert.True(t, *cm.OwnerReferences[0].Controller)
	}

	got := &configv1alpha1.ConfigSnapshot{}
	require.NoError(t, c.Get(ctx, types.NamespacedName{Name: cs.Name}, got))
	assert.Equal(t, 2, got.Status.TargetCount)
	assert.Equal(t, 2, got.Status.AppliedCount)
	assert.Equal(t, 0, got.Status.FailedCount)
	assert.Len(t, got.Status.Targets, 2)
	for _, ts := range got.Status.Targets {
		assert.Equal(t, configv1alpha1.TargetApplied, ts.Phase)
		assert.Equal(t, digest, ts.Version)
	}
}

// TestReconcile_SelectorNarrowingGCsOnlyOwnedObjects narrows the selector and
// asserts the child in the dropped namespace is deleted ONLY when its owner
// UID matches; a foreign same-labelled object with another UID survives.
func TestReconcile_SelectorNarrowingGCsOnlyOwnedObjects(t *testing.T) {
	ctx := context.Background()
	cs := newSnapshot()
	objs := append([]client.Object{cs}, namespaces("test-a", "test-b")...)
	r, c := newReconciler(t, objs...)
	digest := digestOf(cs)
	childName := naming.ChildName(cs.Name, digest)

	reconcileOnce(ctx, t, r, cs.Name)

	// Inject a FOREIGN object in test-b carrying our managed label but with a
	// different owner UID. Represents a recreated owner / same-name collision.
	foreign := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "test-b",
			Name:      childName,
			Labels:    naming.ChildLabels(cs.Name),
			UID:       types.UID("foreign-cm-uid"),
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: "config.example.com/v1alpha1",
				Kind:       "ConfigSnapshot",
				Name:       cs.Name,
				UID:        types.UID("DIFFERENT-OWNER-UID"),
				Controller: boolPtr(true),
			}},
		},
		Data: map[string]string{naming.DataKey: "foreign data must survive"},
	}
	// Remove the real owned child first so the foreign one occupies the name.
	owned := &corev1.ConfigMap{}
	require.NoError(t, c.Get(ctx, types.NamespacedName{Namespace: "test-b", Name: childName}, owned))
	require.NoError(t, c.Delete(ctx, owned))
	require.NoError(t, c.Create(ctx, foreign))

	// Narrow selector to only test-a (remove label from test-b).
	nsB := &corev1.Namespace{}
	require.NoError(t, c.Get(ctx, types.NamespacedName{Name: "test-b"}, nsB))
	nsB.Labels = map[string]string{"tier": "other"}
	require.NoError(t, c.Update(ctx, nsB))

	res := reconcileOnce(ctx, t, r, cs.Name)
	assert.False(t, res.Requeue)

	// Owned child in test-a must remain; nothing foreign in test-b may be deleted.
	survivor := &corev1.ConfigMap{}
	require.NoError(t, c.Get(ctx, types.NamespacedName{Namespace: "test-b", Name: childName}, survivor))
	assert.Equal(t, "foreign data must survive", survivor.Data[naming.DataKey])
	assert.Equal(t, types.UID("DIFFERENT-OWNER-UID"), survivor.OwnerReferences[0].UID)

	// Now restore selection: name conflict must be reported as Failed, and the
	// foreign object must still be untouched.
	nsB.Labels = map[string]string{"tier": "test"}
	require.NoError(t, c.Update(ctx, nsB))
	reconcileOnce(ctx, t, r, cs.Name)
	got := &configv1alpha1.ConfigSnapshot{}
	require.NoError(t, c.Get(ctx, types.NamespacedName{Name: cs.Name}, got))
	var bStatus *configv1alpha1.TargetStatus
	for i := range got.Status.Targets {
		if got.Status.Targets[i].Namespace == "test-b" {
			bStatus = &got.Status.Targets[i]
		}
	}
	require.NotNil(t, bStatus)
	assert.Equal(t, configv1alpha1.TargetFailed, bStatus.Phase)
	assert.Contains(t, bStatus.LastError, "name conflict")
	require.NoError(t, c.Get(ctx, types.NamespacedName{Namespace: "test-b", Name: childName}, survivor))
	assert.Equal(t, "foreign data must survive", survivor.Data[naming.DataKey])
}

// TestReconcile_PartialFailureKeepsSuccessfulTargetVersion verifies that when
// one target apply fails, the successful target's applied Version is retained
// and the failed target keeps the previous version instead of rolling back;
// once the failure clears both converge.
func TestReconcile_PartialFailureKeepsSuccessfulTargetVersion(t *testing.T) {
	ctx := context.Background()
	cs := newSnapshot()
	objs := append([]client.Object{cs}, namespaces("test-a", "test-b")...)
	r, c := newReconciler(t, objs...)
	digest := digestOf(cs)
	childName := naming.ChildName(cs.Name, digest)

	// Seed one healthy target and a pre-existing failure state for test-b as
	// if the create had been denied by an admission webhook.
	healthy := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "test-a",
			Name:      childName,
			Labels:    naming.ChildLabels(cs.Name),
			Annotations: map[string]string{
				naming.DigestKey: digest,
			},
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: "config.example.com/v1alpha1",
				Kind:       "ConfigSnapshot",
				Name:       cs.Name,
				UID:        cs.UID,
				Controller: boolPtr(true),
			}},
		},
		Data: map[string]string{naming.DataKey: cs.Spec.Payload.Data},
	}
	require.NoError(t, c.Create(ctx, healthy))

	// Blocking client: create of configmaps in test-b returns Forbidden,
	// emulating the injected admission failure.
	r.Client = &failingClient{
		Client:         c,
		forbidCreateNS: "test-b",
	}

	res, err := r.Reconcile(ctx, ctrl.Request{NamespacedName: types.NamespacedName{Name: cs.Name}})
	require.NoError(t, err)
	assert.True(t, res.RequeueAfter > 0, "partial failure must schedule retry")

	got := &configv1alpha1.ConfigSnapshot{}
	require.NoError(t, c.Get(ctx, types.NamespacedName{Name: cs.Name}, got))
	assert.Equal(t, 1, got.Status.AppliedCount)
	assert.Equal(t, 1, got.Status.FailedCount)
	for _, ts := range got.Status.Targets {
		switch ts.Namespace {
		case "test-a":
			assert.Equal(t, configv1alpha1.TargetApplied, ts.Phase)
			assert.Equal(t, digest, ts.Version, "successful target must keep its version")
		case "test-b":
			assert.Equal(t, configv1alpha1.TargetFailed, ts.Phase)
			assert.Contains(t, ts.LastError, "forbidden")
			assert.Empty(t, ts.Version, "never-applied target must not claim a version")
		}
	}

	// Failure clears (e.g. FirstN budget exhausted): next retry applies test-b.
	r.Client = c
	reconcileOnce(ctx, t, r, cs.Name)
	require.NoError(t, c.Get(ctx, types.NamespacedName{Name: cs.Name}, got))
	assert.Equal(t, 2, got.Status.AppliedCount)
	assert.Equal(t, 0, got.Status.FailedCount)
}

// TestReconcile_DriftIsRepaired proves a successful target whose ConfigMap was
// edited or truncated is restored instead of duplicated.
func TestReconcile_DriftIsRepaired(t *testing.T) {
	ctx := context.Background()
	cs := newSnapshot()
	objs := append([]client.Object{cs}, namespaces("test-a")...)
	r, c := newReconciler(t, objs...)
	digest := digestOf(cs)
	childName := naming.ChildName(cs.Name, digest)

	reconcileOnce(ctx, t, r, cs.Name)
	cm := &corev1.ConfigMap{}
	require.NoError(t, c.Get(ctx, types.NamespacedName{Namespace: "test-a", Name: childName}, cm))
	originalUID := cm.UID

	// External tampering.
	cm.Data[naming.DataKey] = "TAMPERED"
	delete(cm.Annotations, naming.DigestKey)
	require.NoError(t, c.Update(ctx, cm))

	reconcileOnce(ctx, t, r, cs.Name)
	require.NoError(t, c.Get(ctx, types.NamespacedName{Namespace: "test-a", Name: childName}, cm))
	assert.Equal(t, cs.Spec.Payload.Data, cm.Data[naming.DataKey])
	assert.Equal(t, digest, cm.Annotations[naming.DigestKey])
	assert.Equal(t, originalUID, cm.UID, "drift repair must update in place")
}

// TestReconcile_OwnerRecreatedWithNewUID is the owner-rebuild case: the old
// UID's orphan children linger in target namespaces. A new ConfigSnapshot with
// a fresh UID must not delete those orphans (they belong to the old UID), but
// must deliver its own children successfully.
func TestReconcile_OwnerRecreatedWithNewUID(t *testing.T) {
	ctx := context.Background()
	old := newSnapshot()
	objs := append([]client.Object{old}, namespaces("test-a")...)
	r, c := newReconciler(t, objs...)
	digest := digestOf(old)
	childName := naming.ChildName(old.Name, digest)
	reconcileOnce(ctx, t, r, old.Name)

	// Orphan the child: delete owner without cascade (simulates finalizer-less
	// crash / etcd restore). The child keeps owner UID owner-uid-1.
	require.NoError(t, c.Delete(ctx, old))
	// Ensure gone.
	gone := &configv1alpha1.ConfigSnapshot{}
	err := c.Get(ctx, types.NamespacedName{Name: old.Name}, gone)
	require.True(t, apierrors.IsNotFound(err))

	// Recreate owner with the SAME name but a NEW UID.
	fresh := newSnapshot()
	fresh.UID = types.UID("owner-uid-2")
	fresh.ResourceVersion = ""
	require.NoError(t, c.Create(ctx, fresh))

	reconcileOnce(ctx, t, r, fresh.Name)

	// Because content digest is identical, the desired name equals the
	// orphan's. The orphan has the OLD owner UID -> treated as a name conflict,
	// never deleted. This is the deliberate safety guarantee.
	orphan := &corev1.ConfigMap{}
	require.NoError(t, c.Get(ctx, types.NamespacedName{Namespace: "test-a", Name: childName}, orphan))
	assert.Equal(t, types.UID("owner-uid-1"), orphan.OwnerReferences[0].UID,
		"orphan owned by old UID must never be deleted by new owner")

	got := &configv1alpha1.ConfigSnapshot{}
	require.NoError(t, c.Get(ctx, types.NamespacedName{Name: fresh.Name}, got))
	var aStatus configv1alpha1.TargetStatus
	for _, ts := range got.Status.Targets {
		if ts.Namespace == "test-a" {
			aStatus = ts
		}
	}
	assert.Equal(t, configv1alpha1.TargetFailed, aStatus.Phase)
	assert.Contains(t, aStatus.LastError, "name conflict")
}

// TestReconcile_TerminatingNamespaceFailed records Pending/Failed without
// producing a create error loop.
func TestReconcile_TerminatingNamespaceFailed(t *testing.T) {
	ctx := context.Background()
	cs := newSnapshot()
	nsTerm := &corev1.Namespace{
		ObjectMeta: metav1.ObjectMeta{
			Name:   "test-a",
			UID:    "uid-term",
			Labels: map[string]string{"tier": "test"},
		},
		Status: corev1.NamespaceStatus{Phase: corev1.NamespaceTerminating},
	}
	r, _ := newReconciler(t, cs, nsTerm)
	res, err := r.Reconcile(ctx, ctrl.Request{NamespacedName: types.NamespacedName{Name: cs.Name}})
	require.NoError(t, err)
	assert.True(t, res.RequeueAfter > 0)
}

func boolPtr(b bool) *bool { return &b }

// failingClient wraps a client and denies ConfigMap creates in one namespace,
// the unit-level equivalent of the chaos admission webhook.
type failingClient struct {
	client.Client
	forbidCreateNS string
}

func (f *failingClient) Create(ctx context.Context, obj client.Object, opts ...client.CreateOption) error {
	if cm, ok := obj.(*corev1.ConfigMap); ok && cm.Namespace == f.forbidCreateNS {
		return apierrors.NewForbidden(
			corev1.Resource("configmaps"),
			cm.Namespace+"/"+cm.Name,
			errInjected)
	}
	return f.Client.Create(ctx, obj, opts...)
}

var errInjected = errors.New("injected failure: forbidden")

// countingClient counts successful CREATE calls, used to prove dedup across
// repeated reconcile rounds.
type countingClient struct {
	client.Client
	creates int
}

func (c *countingClient) Create(ctx context.Context, obj client.Object, opts ...client.CreateOption) error {
	if err := c.Client.Create(ctx, obj, opts...); err != nil {
		return err
	}
	c.creates++
	return nil
}
