package integration

import (
	"context"
	"testing"
	"time"

	admissionregistrationv1 "k8s.io/api/admissionregistration/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	quota "resourcequota-reservation/api/v1alpha1"
)

// admissionCfg bundles the live ValidatingWebhookConfiguration with a snapshot
// used to temporarily remove (and then restore) an admission webhook in the
// expiry/late-pod race test.
type admissionCfg struct {
	object   admissionregistrationv1.ValidatingWebhookConfiguration
	snapshot *admissionregistrationv1.ValidatingWebhookConfiguration
}

func parseDur(t *testing.T, s string) metav1.Duration {
	t.Helper()
	d, err := time.ParseDuration(s)
	if err != nil {
		t.Fatalf("bad duration %q: %v", s, err)
	}
	return metav1.Duration{Duration: d}
}

// podForClaim builds a Pod labeled with a claim and requesting cpu/mem.
func podForClaim(ns, name, claimName, cpu, mem string) *corev1.Pod {
	p := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: ns,
			Name:      name,
			Labels:    map[string]string{quota.ClaimLabelKey: claimName},
		},
		Spec: corev1.PodSpec{
			RestartPolicy: corev1.RestartPolicyNever,
			Containers: []corev1.Container{{
				Name:    "workload",
				Image:   "registry.k8s.io/pause:3.10",
				Command: []string{"/pause"},
				Resources: corev1.ResourceRequirements{
					Requests: corev1.ResourceList{
						corev1.ResourceCPU:    resource.MustParse(cpu),
						corev1.ResourceMemory: resource.MustParse(mem),
					},
				},
			}},
		},
	}
	return p
}

func createPod(t *testing.T, p *corev1.Pod) error {
	t.Helper()
	err := directClient.Create(context.Background(), p)
	return err
}

func cleanupPod(t *testing.T, name, ns string) {
	t.Helper()
	p := &corev1.Pod{}
	p.Namespace = ns
	p.Name = name
	_ = directClient.Delete(context.Background(), p)
}

func isForbidden(err error) bool {
	return apierrors.IsForbidden(err) || apierrors.IsInvalid(err)
}
