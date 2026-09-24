package failinject

import (
	"testing"

	admissionv1 "k8s.io/api/admission/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

func req(ns, name string, op admissionv1.Operation) *admissionv1.AdmissionRequest {
	return &admissionv1.AdmissionRequest{
		UID:       types.UID("req-" + ns + "-" + name),
		Operation: op,
		Namespace: ns,
		Name:      name,
		Resource:  metav1.GroupVersionResource{Resource: "configmaps"},
	}
}

func TestMatcher_FirstNRecovers(t *testing.T) {
	m := NewMatcher()
	m.ReplacePolicies([]FailPolicy{{
		ObjectMeta: metav1.ObjectMeta{Name: "p"},
		Spec: FailPolicySpec{
			TargetNamespaces: []string{"test-b"},
			Actions:          []string{ActionCreate},
			Mode:             ModeFirstN,
			FirstN:           2,
		},
	}})
	r := req("test-b", "cfg-x", admissionv1.Create)
	if !m.Evaluate(r).Deny {
		t.Fatal("first request must be denied")
	}
	if !m.Evaluate(r).Deny {
		t.Fatal("second request must be denied")
	}
	if d := m.Evaluate(r); d.Deny {
		t.Fatal("third request must pass: injected failure recovered")
	}
	// Other namespaces unaffected.
	if d := m.Evaluate(req("test-a", "cfg-x", admissionv1.Create)); d.Deny {
		t.Fatal("policy must not apply to test-a")
	}
}

func TestMatcher_NamespaceSelector(t *testing.T) {
	m := NewMatcher()
	m.ReplaceNamespaces([]corev1.Namespace{
		{ObjectMeta: metav1.ObjectMeta{Name: "test-a", Labels: map[string]string{"tier": "test"}}},
		{ObjectMeta: metav1.ObjectMeta{Name: "test-b", Labels: map[string]string{"tier": "other"}}},
	})
	m.ReplacePolicies([]FailPolicy{{
		ObjectMeta: metav1.ObjectMeta{Name: "p"},
		Spec: FailPolicySpec{
			NamespaceSelector: map[string]string{"tier": "test"},
			Mode:              ModeAlways,
		},
	}})
	if !m.Evaluate(req("test-a", "cm", admissionv1.Create)).Deny {
		t.Fatal("test-a matches selector: deny")
	}
	if m.Evaluate(req("test-b", "cm", admissionv1.Create)).Deny {
		t.Fatal("test-b does not match selector: allow")
	}
}

func TestMatcher_ObjectNameSubstringAndActions(t *testing.T) {
	m := NewMatcher()
	m.ReplacePolicies([]FailPolicy{{
		ObjectMeta: metav1.ObjectMeta{Name: "p"},
		Spec: FailPolicySpec{
			ObjectNameSubstring: "cfg-",
			Actions:             []string{ActionDelete},
			Mode:                ModeAlways,
		},
	}})
	if !m.Evaluate(req("ns", "cfg-abc", admissionv1.Delete)).Deny {
		t.Fatal("delete of cfg- object must be denied")
	}
	if m.Evaluate(req("ns", "cfg-abc", admissionv1.Create)).Deny {
		t.Fatal("create is not in actions: allow")
	}
	if m.Evaluate(req("ns", "other", admissionv1.Delete)).Deny {
		t.Fatal("non-matching name: allow")
	}
}

func TestMatcher_ReplacesAreAtomicAndCounterPreserved(t *testing.T) {
	m := NewMatcher()
	m.ReplacePolicies([]FailPolicy{{
		ObjectMeta: metav1.ObjectMeta{Name: "p"},
		Spec:       FailPolicySpec{Mode: ModeFirstN, FirstN: 3},
	}})
	m.Evaluate(req("ns", "cm", admissionv1.Create))
	// Policy update (e.g. message change) preserves the budget counter.
	m.ReplacePolicies([]FailPolicy{{
		ObjectMeta: metav1.ObjectMeta{Name: "p"},
		Spec: FailPolicySpec{
			Mode:    ModeFirstN,
			FirstN:  3,
			Message: "updated",
		},
	}})
	m.Evaluate(req("ns", "cm", admissionv1.Create))
	if got := m.Consumed("p"); got != 2 {
		t.Fatalf("counter must survive policy replace, got %d", got)
	}
	// Removing the policy drops it.
	m.ReplacePolicies(nil)
	if m.Evaluate(req("ns", "cm", admissionv1.Create)).Deny {
		t.Fatal("no policies: allow")
	}
}
