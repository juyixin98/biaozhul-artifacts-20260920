package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	coordinatorv1alpha1 "github.com/example/certrenewal/api/v1alpha1"
	"github.com/example/certrenewal/internal/ca"
	"github.com/example/certrenewal/internal/clock"
	"github.com/example/certrenewal/internal/pki"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"
)

// Signer performs the CA signing step. It is a seam used to inject failures in
// unit tests; production wiring uses SignWithCA.
type Signer interface {
	Sign(ctx context.Context, req *coordinatorv1alpha1.CertificateRequest, loaded *ca.CA) (*pki.SignResult, error)
}

// SignWithCA is the production Signer: validates the CSR against the live
// Certificate's domains and signs it. The domain list is read from the owning
// Certificate (found via owner annotations), so signing never trusts SANs
// claimed by the request alone.
type SignWithCA struct {
	Client client.Client
	Clk    clock.Clock
}

// Sign implements Signer.
func (s *SignWithCA) Sign(ctx context.Context, cr *coordinatorv1alpha1.CertificateRequest, loaded *ca.CA) (*pki.SignResult, error) {
	owner := cr.Annotations[AnnotationOwnerName]
	if owner == "" {
		return nil, errors.New("certificate request is missing owner annotation")
	}
	cert := &coordinatorv1alpha1.Certificate{}
	if err := s.Client.Get(ctx, types.NamespacedName{Namespace: cr.Namespace, Name: owner}, cert); err != nil {
		return nil, fmt.Errorf("load owning certificate: %w", err)
	}
	now := s.Clk.Now()
	return pki.SignCSR(
		loaded.Certificate, loaded.Key,
		cr.Spec.Request,
		cert.Spec.CommonName, cert.Spec.DNSNames,
		now, now.Add(cr.Spec.Duration.Duration),
	)
}

// CertificateRequestReconciler signs pending CertificateRequests.
type CertificateRequestReconciler struct {
	client.Client
	Scheme *runtime.Scheme
	CAs    ca.Source
	Signer Signer
	Clk    clock.Clock

	// failureBackoff returns the requeue delay for the n-th consecutive
	// failure (kept small on purpose; tests override it).
	FailureBackoff func(failures int32) time.Duration
}

// maxRequeue caps retries when the CA is broken.
const maxRequeue = 5 * time.Minute

func defaultBackoff(n int32) time.Duration {
	d := time.Duration(1<<uint(min(n, 10))) * time.Second // 2s .. 1024s
	if d > maxRequeue {
		d = maxRequeue
	}
	return d
}

// Reconcile performs one signing attempt.
func (r *CertificateRequestReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	cr := &coordinatorv1alpha1.CertificateRequest{}
	if err := r.Get(ctx, req.NamespacedName, cr); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	if cond, ok := readyCondition(cr.Status.Conditions); ok && cond.Status == "True" {
		return ctrl.Result{}, nil // already signed, nothing to do
	}

	loaded, err := r.CAs.Get(ctx, cr.Namespace, cr.Spec.IssuerRef.Name)
	if err != nil {
		return r.fail(ctx, cr, coordinatorv1alpha1.ReasonCAUnavailable, err, false)
	}

	signed, err := r.Signer.Sign(ctx, cr, loaded)
	if err != nil {
		// CSR content errors are permanent until the request is replaced;
		// everything else retries.
		permanent := errors.Is(err, errPermanentCSR) || isCSRValidationError(err)
		logger.Info("certificate request signing failed", "permanent", permanent, "error", err.Error())
		return r.fail(ctx, cr, coordinatorv1alpha1.ReasonCSRInvalid, err, permanent)
	}

	// Write the signed receipt into status. A status update conflict means a
	// concurrent update won; the work is idempotent, so simply requeue.
	cr.Status.Certificate = signed.CertificatePEM
	cr.Status.CA = signed.CAPEM
	cr.Status.SerialNumber = pki.SerialHex(signed.Certificate)
	t := metav1Time(signed.Certificate.NotAfter)
	cr.Status.NotAfter = &t
	t2 := metav1Time(signed.Certificate.NotBefore)
	cr.Status.NotBefore = &t2
	cr.Status.FailureCount = 0
	setCondition(&cr.Status.Conditions, cr.Generation, coordinatorv1alpha1.ConditionReady,
		"True", coordinatorv1alpha1.ReasonSigned,
		fmt.Sprintf("signed by CA %s, serial %s", cr.Spec.IssuerRef.Name, cr.Status.SerialNumber))
	if err := r.Status().Update(ctx, cr); err != nil {
		logger.Info("certificate request status update conflict, requeuing", "error", err.Error())
		return ctrl.Result{Requeue: true}, nil
	}
	logger.Info("certificate request signed", "serial", cr.Status.SerialNumber, "notAfter", signed.Certificate.NotAfter)
	return ctrl.Result{}, nil
}

func (r *CertificateRequestReconciler) fail(ctx context.Context, cr *coordinatorv1alpha1.CertificateRequest, reason string, cause error, permanent bool) (ctrl.Result, error) {
	logger := log.FromContext(ctx)
	cr.Status.FailureCount++
	status := "False"
	if permanent {
		setCondition(&cr.Status.Conditions, cr.Generation, coordinatorv1alpha1.ConditionReady,
			status, reason, cause.Error())
		_ = r.Status().Update(ctx, cr)
		// Keep a slow requeue even for permanent errors so a fixed CSR/CA
		// relationship recovers without manual intervention.
		return ctrl.Result{RequeueAfter: maxRequeue}, nil
	}
	setCondition(&cr.Status.Conditions, cr.Generation, coordinatorv1alpha1.ConditionReady,
		status, reason, cause.Error())
	if err := r.Status().Update(ctx, cr); err != nil {
		return ctrl.Result{Requeue: true}, nil
	}
	backoff := defaultBackoff
	if r.FailureBackoff != nil {
		backoff = r.FailureBackoff
	}
	d := backoff(cr.Status.FailureCount)
	logger.Info("signing failed, will retry", "failures", cr.Status.FailureCount, "requeueAfter", d, "error", cause.Error())
	return ctrl.Result{RequeueAfter: d}, nil
}

// SetupWithManager registers the reconciler.
func (r *CertificateRequestReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&coordinatorv1alpha1.CertificateRequest{}).
		Complete(r)
}
