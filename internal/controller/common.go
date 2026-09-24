package controller

import (
	"fmt"
	"time"

	coordinatorv1alpha1 "github.com/example/certrenewal/api/v1alpha1"
	"github.com/example/certrenewal/internal/pki"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Labels / annotations stamped onto owned secrets and certificate requests.
const (
	LabelOwnerName = "coordinator.example.com/certificate-name"
	LabelSpecHash  = "coordinator.example.com/spec-hash"

	// AnnotationSpecHash on a CertificateRequest binds the receipt to the
	// exact spec inputs it was created for.
	AnnotationSpecHash = "coordinator.example.com/spec-hash"
	// AnnotationOwnerName records the owning Certificate (requests are not
	// garbage-collected via owner refs so they remain auditable).
	AnnotationOwnerName = "coordinator.example.com/certificate-name"
	// AnnotationFailureCount is mirrored for cheap ordering during backoff.
	lastFailureAnnotation = "coordinator.example.com/last-failure"

	// clockSkewTolerance guards renewal decisions against a node clock that
	// is up to this much ahead of real time.
	clockSkewTolerance = 30 * time.Second
)

// TLS Secret key names used by the coordinator (same as kubernetes.io/tls).
var (
	tlsCertKey = corev1.TLSCertKey       // tls.crt
	tlsKeyKey  = corev1.TLSPrivateKeyKey // tls.key
	caCertKey  = corev1.ServiceAccountRootCAKey
)

func setCondition(conds *[]metav1.Condition, gen int64, typ, status, reason, msg string) {
	meta.SetStatusCondition(conds, metav1.Condition{
		Type:               typ,
		Status:             metav1.ConditionStatus(status),
		ObservedGeneration: gen,
		Reason:             reason,
		Message:            msg,
	})
}

func readyCondition(conds []metav1.Condition) (metav1.Condition, bool) {
	for _, c := range conds {
		if c.Type == coordinatorv1alpha1.ConditionReady {
			return c, true
		}
	}
	return metav1.Condition{}, false
}

// normalizeSpec validates the spec and returns the effective duration and
// renew-before. The renewal window is always a fraction bound to the actual
// certificate lifetime; renewBefore >= duration is rejected.
func normalizeSpec(spec coordinatorv1alpha1.CertificateSpec) (duration, renewBefore time.Duration, err error) {
	duration = pki.DefaultDuration
	if spec.Duration != nil && spec.Duration.Duration > 0 {
		duration = spec.Duration.Duration
	}
	if duration < pki.MinDuration {
		return 0, 0, fmt.Errorf("duration %s is below minimum %s", duration, pki.MinDuration)
	}

	renewBefore = duration / pki.DefaultRenewFrac
	if spec.RenewBefore != nil && spec.RenewBefore.Duration > 0 {
		renewBefore = spec.RenewBefore.Duration
	}
	if renewBefore >= duration {
		return 0, 0, fmt.Errorf("renewBefore %s must be smaller than duration %s", renewBefore, duration)
	}
	if len(spec.DNSNames) == 0 {
		return 0, 0, fmt.Errorf("dnsNames must contain at least one name")
	}
	if spec.SecretName == "" {
		return 0, 0, fmt.Errorf("secretName is required")
	}
	if spec.IssuerRef.Name == "" {
		return 0, 0, fmt.Errorf("issuerRef.name is required")
	}
	for _, n := range spec.DNSNames {
		if n == "" {
			return 0, 0, fmt.Errorf("dnsNames must not contain empty entries")
		}
	}
	return duration, renewBefore, nil
}
