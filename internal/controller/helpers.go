package controller

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"io"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	certv1alpha1 "github.com/example/cert-renewal-operator/api/v1alpha1"
	"github.com/example/cert-renewal-operator/internal/pki"
)

// keyFor is a compact namespaced-name for status retry lookups.
func keyOf(cr *certv1alpha1.Certificate) types.NamespacedName {
	return types.NamespacedName{Namespace: cr.Namespace, Name: cr.Name}
}

// mutateStatus re-reads the object, applies mutate to a copy of its status,
// and writes the status subresource, retrying on optimistic-concurrency.
func (r *Reconciler) mutateStatus(ctx context.Context, nn types.NamespacedName,
	mutate func(*certv1alpha1.CertificateStatus, int64)) error {
	for attempt := 0; attempt < maxConflictRetries; attempt++ {
		latest := &certv1alpha1.Certificate{}
		if err := r.Get(ctx, nn, latest); err != nil {
			if apierrors.IsNotFound(err) {
				return nil
			}
			return err
		}
		desired := latest.DeepCopy()
		mutate(&desired.Status, desired.Generation)
		desired.Status.ObservedGeneration = desired.Generation
		if err := r.Status().Update(ctx, desired); err != nil {
			if apierrors.IsConflict(err) {
				continue
			}
			return err
		}
		return nil
	}
	return fmt.Errorf("status update exceeded %d conflict retries for %s", maxConflictRetries, nn)
}

func setCondition(st *certv1alpha1.CertificateStatus, now time.Time,
	t certv1alpha1.CertificateStatusType, cond metav1.ConditionStatus, reason, msg string) {
	meta.SetStatusCondition(&st.Conditions, metav1.Condition{
		Type:               string(t),
		Status:             cond,
		ObservedGeneration: 0, // set by caller's object generation context; keep stable list
		LastTransitionTime: metav1.NewTime(now.UTC()),
		Reason:             reason,
		Message:            truncate(msg),
	})
}

func truncate(m string) string {
	const max = 1024
	if len(m) > max {
		return m[:max]
	}
	return m
}

func (r *Reconciler) markInvalid(ctx context.Context, cr *certv1alpha1.Certificate, specErr error) error {
	now := r.clock().Now()
	return r.mutateStatus(ctx, keyOf(cr), func(st *certv1alpha1.CertificateStatus, _ int64) {
		setCondition(st, now, certv1alpha1.ConditionIssuing, metav1.ConditionFalse, certv1alpha1.ReasonSpecInvalid, specErr.Error())
		setCondition(st, now, certv1alpha1.ConditionReady, metav1.ConditionFalse, certv1alpha1.ReasonSpecInvalid, specErr.Error())
	})
}

func (r *Reconciler) setReady(ctx context.Context, cr *certv1alpha1.Certificate,
	ready bool, reason, msg string, base certv1alpha1.CertificateStatus) error {
	return r.setReadyWithStatus(ctx, cr, ready, reason, msg, base)
}

func (r *Reconciler) setReadyWithStatus(ctx context.Context, cr *certv1alpha1.Certificate,
	ready bool, reason, msg string, base certv1alpha1.CertificateStatus) error {
	now := r.clock().Now()
	return r.mutateStatus(ctx, keyOf(cr), func(st *certv1alpha1.CertificateStatus, _ int64) {
		// Preserve the summary of whatever cert is still serving.
		st.NotBefore = base.NotBefore
		st.NotAfter = base.NotAfter
		st.Serial = base.Serial
		st.Thumbprint = base.Thumbprint
		st.SpecHash = base.SpecHash
		st.CurrentSecret = base.CurrentSecret
		st.RenewalTime = base.RenewalTime
		readyCond := metav1.ConditionFalse
		if ready {
			readyCond = metav1.ConditionTrue
		}
		setCondition(st, now, certv1alpha1.ConditionIssuing, metav1.ConditionFalse, reason, msg)
		setCondition(st, now, certv1alpha1.ConditionReady, readyCond, reason, msg)
	})
}

func (r *Reconciler) reflectIssuing(ctx context.Context, cr *certv1alpha1.Certificate,
	serving *corev1.Secret, servingLeaf *x509.Certificate, specHash string,
	eff certv1alpha1.EffectiveSpec, reason, msg string, now time.Time) error {
	return r.mutateStatus(ctx, keyOf(cr), func(st *certv1alpha1.CertificateStatus, _ int64) {
		setCondition(st, now, certv1alpha1.ConditionIssuing, metav1.ConditionTrue, reason, msg)
		if servingLeaf != nil {
			// Old cert still valid -> Ready remains True while renewing.
			st.NotBefore = metav1.Time{Time: servingLeaf.NotBefore}
			st.NotAfter = metav1.Time{Time: servingLeaf.NotAfter}
			st.Serial = pki.SerialString(servingLeaf.SerialNumber)
			st.Thumbprint = pki.ThumbprintSHA256(servingLeaf.Raw)
			if serving != nil {
				st.SpecHash = serving.Annotations[annSpecHash]
			}
			st.CurrentSecret = eff.SecretName
			rt := pki.RenewalTime(servingLeaf.NotAfter, now, eff.RenewBefore)
			st.RenewalTime = &metav1.Time{Time: rt}
			setCondition(st, now, certv1alpha1.ConditionReady, metav1.ConditionTrue,
				certv1alpha1.ReasonPending, "renewal in progress; previous certificate still valid")
		} else {
			setCondition(st, now, certv1alpha1.ConditionReady, metav1.ConditionFalse, reason, msg)
		}
		_ = specHash
	})
}

func (r *Reconciler) syncStatusFromSecret(ctx context.Context, cr *certv1alpha1.Certificate,
	sec *corev1.Secret, leaf *x509.Certificate, specHash string, eff certv1alpha1.EffectiveSpec,
	renewalAt time.Time, ready bool, reason, msg string, now time.Time) error {
	return r.mutateStatus(ctx, keyOf(cr), func(st *certv1alpha1.CertificateStatus, _ int64) {
		if leaf != nil {
			st.NotBefore = metav1.Time{Time: leaf.NotBefore}
			st.NotAfter = metav1.Time{Time: leaf.NotAfter}
			st.Serial = pki.SerialString(leaf.SerialNumber)
			st.Thumbprint = pki.ThumbprintSHA256(leaf.Raw)
		}
		st.SpecHash = specHash
		st.CurrentSecret = eff.SecretName
		st.RenewalTime = &metav1.Time{Time: renewalAt}
		readyCond := metav1.ConditionFalse
		if ready {
			readyCond = metav1.ConditionTrue
		}
		setCondition(st, now, certv1alpha1.ConditionIssuing, metav1.ConditionFalse, reason, msg)
		setCondition(st, now, certv1alpha1.ConditionReady, readyCond, reason, msg)
	})
}

// ---- small crypto/pem indirections (keep reconcile.go import list small) ----

func ellipticP256() elliptic.Curve { return elliptic.P256() }
func randReader() io.Reader        { return rand.Reader }

func pemEncodePrivateKey(der []byte) []byte {
	return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})
}

func pemDecode(data []byte) (*pem.Block, []byte) { return pem.Decode(data) }

func publicKeyMatches(cert *x509.Certificate, key *ecdsa.PrivateKey) bool {
	return spkiEqual(cert.PublicKey, &key.PublicKey)
}

func publicKeyMatchesFromCSR(csr *x509.CertificateRequest, key *ecdsa.PrivateKey) bool {
	return spkiEqual(csr.PublicKey, &key.PublicKey)
}

func spkiEqual(a, b any) bool {
	ab, err := x509.MarshalPKIXPublicKey(a)
	if err != nil {
		return false
	}
	bb, err := x509.MarshalPKIXPublicKey(b)
	if err != nil {
		return false
	}
	if len(ab) != len(bb) {
		return false
	}
	for i := range ab {
		if ab[i] != bb[i] {
			return false
		}
	}
	return true
}
