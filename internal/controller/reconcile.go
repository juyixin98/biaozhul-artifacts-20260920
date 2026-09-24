// Package controller implements the Certificate reconciler.
//
// Safety properties enforced here:
//
//  1. Renewal window is bound to the actual issued certificate's NotAfter
//     (notAfter - renewBefore - clockSkewTolerance), not to a wall timer.
//  2. On issuance failure the serving Secret is never deleted or overwritten;
//     if the old cert still verifies, Ready stays True.
//  3. In-flight CSR + private key persist in a "-pending" Secret. Repeated
//     reconcile and process restart reuse the same pending application
//     instead of minting a new key for every attempt.
//  4. Before an issuance result is committed the live Certificate is
//     re-read; a generation/spec mismatch causes the receipt to be discarded
//     (old-generation receipt can never overwrite a newer DNS configuration).
//  5. Secret content (tls.crt), serial and status summary all derive from the
//     same parsed certificate and are re-verified from the API read-back.
//
// Private keys are written only into Secret data; they are never logged.
package controller

import (
	"context"
	"crypto/ecdsa"
	"crypto/x509"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	certv1alpha1 "github.com/example/cert-renewal-operator/api/v1alpha1"
	"github.com/example/cert-renewal-operator/internal/pki"
)

const (
	// annotation keys on serving and pending Secrets.
	annSerial      = "certificates.example.com/serial"
	annThumbprint  = "certificates.example.com/thumbprint"
	annSpecHash    = "certificates.example.com/spec-hash"
	annGeneration  = "certificates.example.com/generation"
	annPendingCSR  = "certificates.example.com/pending-csr-spec-hash"
	annNotAfter    = "certificates.example.com/not-after"
	pendingSuffix  = "-pending"
	tlsCertKey     = corev1.TLSCertKey       // tls.crt
	tlsPrivateKey  = corev1.TLSPrivateKeyKey // tls.key
	tlsCABundleKey = "ca.crt"

	// maxConflictRetries bounds optimistic-concurrency retries inside one reconcile.
	maxConflictRetries = 5
)

// Clock allows tests to drive time.
type Clock interface {
	Now() time.Time
}

type realClock struct{}

func (realClock) Now() time.Time { return time.Now() }

// Signer performs the actual cryptographic issuance. The pending key is
// passed in when a pending application is being resumed (so restarts/requeues
// reuse the same key); it is nil on the first attempt.
type Signer interface {
	Sign(ctx context.Context, ca *pki.CA, dnsNames []string, duration time.Duration, pendingKey *ecdsa.PrivateKey, now time.Time) (*pki.IssueResult, error)
}

// Reconciler reconciles Certificate objects.
type Reconciler struct {
	client.Client
	Scheme     *runtime.Scheme
	Clock      Clock
	Signer     Signer
	Log        logr.Logger
	MaxRequeue time.Duration

	// BeforeCommitSecret, if set, runs immediately before the serving Secret
	// is written (after successful signing) — tests use it to mutate the
	// object to force an optimistic-concurrency conflict.
	BeforeCommitSecret func()
}

// +kubebuilder:rbac:groups=certificates.example.com,resources=certificates,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=certificates.example.com,resources=certificates/status,verbs=get;update;patch
// +kubebuilder:rbac:groups="",resources=secrets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch

func (r *Reconciler) clock() Clock {
	if r.Clock == nil {
		return realClock{}
	}
	return r.Clock
}

func (r *Reconciler) maxRequeue() time.Duration {
	if r.MaxRequeue > 0 {
		return r.MaxRequeue
	}
	return time.Hour
}

// Reconcile is the core loop.
func (r *Reconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := r.Log.WithValues("certificate", req.NamespacedName)
	now := r.clock().Now()

	cr := &certv1alpha1.Certificate{}
	if err := r.Get(ctx, req.NamespacedName, cr); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}
	if !cr.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil
	}

	eff, specErr := cr.Spec.Normalize()
	if specErr != nil {
		return ctrl.Result{}, r.markInvalid(ctx, cr, specErr)
	}

	// CA must exist and parse; a missing/broken CA does NOT touch the serving Secret.
	ca, caErr := r.loadCA(ctx, cr.Namespace, eff.IssuerName)
	if caErr != nil {
		_ = r.setReady(ctx, cr, false, certv1alpha1.ReasonCAUnavailable, caErr.Error(), cr.Status)
		log.Info("ca unavailable; serving secret left untouched", "error", caErr.Error())
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	}

	// Read the current serving Secret (may be absent).
	serving := &corev1.Secret{}
	servingErr := r.Get(ctx, types.NamespacedName{Namespace: cr.Namespace, Name: eff.SecretName}, serving)
	switch {
	case apierrors.IsNotFound(servingErr):
		serving = nil
	case servingErr != nil:
		return ctrl.Result{}, servingErr
	}

	var servingLeaf *x509.Certificate
	servingValid := false
	if serving != nil && serving.Type == corev1.SecretTypeTLS {
		if leaf, err := pki.ParseCertificatePEM(serving.Data[tlsCertKey]); err == nil {
			if vErr := pki.VerifyLeaf(leaf, ca.Certificate, eff.DNSNames, now); vErr == nil {
				servingLeaf = leaf
				servingValid = true
			} else {
				log.Info("serving cert present but no longer acceptable", "reason", vErr.Error())
			}
		}
	}

	specHash := eff.Hash()

	// Decide renewal need from the REAL certificate window.
	needIssue := false
	reason := ""
	switch {
	case serving == nil:
		needIssue, reason = true, "no serving secret"
	case serving.Type != corev1.SecretTypeTLS:
		needIssue, reason = true, "serving secret is not of type kubernetes.io/tls"
	case servingLeaf == nil:
		needIssue, reason = true, "serving certificate missing or unparseable"
	case serving.Annotations[annSpecHash] != specHash:
		needIssue, reason = true, "spec changed (specHash mismatch)"
	default:
		needIssue, reason = pki.NeedsRenewal(servingLeaf, ca.Certificate, eff.DNSNames, eff.RenewBefore, now)
	}

	if !needIssue {
		// Healthy: ensure status mirrors the secret and requeue exactly at window.
		renewalAt := pki.RenewalTime(servingLeaf.NotAfter, now, eff.RenewBefore)
		if err := r.syncStatusFromSecret(ctx, cr, serving, servingLeaf, specHash, eff, renewalAt, true,
			certv1alpha1.ReasonIssued, "certificate valid", now); err != nil {
			return ctrl.Result{}, err
		}
		wait := time.Until(renewalAt)
		if wait < 0 {
			wait = 0
		}
		if wait > r.maxRequeue() {
			wait = r.maxRequeue()
		}
		return ctrl.Result{RequeueAfter: wait}, nil
	}

	log.Info("issuance required", "reason", reason)

	// ----- Pending application handling (resume key across retries/restarts) -----
	pendingName := eff.SecretName + pendingSuffix
	pending := &corev1.Secret{}
	pendingErr := r.Get(ctx, types.NamespacedName{Namespace: cr.Namespace, Name: pendingName}, pending)
	switch {
	case apierrors.IsNotFound(pendingErr):
		pending = nil
	case pendingErr != nil:
		return ctrl.Result{}, pendingErr
	}

	var pendingKey *ecdsa.PrivateKey
	reused := false
	stale := false
	if pending != nil {
		if pending.Annotations[annPendingCSR] != specHash ||
			pending.Annotations[annGeneration] != fmt.Sprintf("%d", cr.Generation) {
			stale = true
		} else {
			if key, err := pki.ParsePrivateKeyPEM(pending.Data[tlsPrivateKey]); err == nil {
				if ecKey, ok := key.(*ecdsa.PrivateKey); ok {
					if csrOK := r.csrMatches(pending.Data["csr.pem"], ecKey, eff.DNSNames); csrOK {
						pendingKey = ecKey
						reused = true
					}
				}
			}
		}
	}

	// Drop stale pending state (older spec/generation or corrupt key).
	if pending != nil && (stale || pendingKey == nil && !reused) {
		if delErr := r.Delete(ctx, pending); delErr != nil && !apierrors.IsNotFound(delErr) {
			return ctrl.Result{}, delErr
		}
		pending = nil
		pendingKey = nil
		reused = false
	}

	// Create a fresh pending application (new key + CSR) when none is reusable.
	if pendingKey == nil {
		newKey, err := ecdsa.GenerateKey(ellipticP256(), randReader())
		if err != nil {
			return ctrl.Result{}, fmt.Errorf("generate pending key: %w", err)
		}
		csrPEM, err := pki.CSRFromRequest(eff.DNSNames, newKey)
		if err != nil {
			return ctrl.Result{}, fmt.Errorf("build csr: %w", err)
		}
		keyDER, err := x509.MarshalPKCS8PrivateKey(newKey)
		if err != nil {
			return ctrl.Result{}, err
		}
		pemKey := pemEncodePrivateKey(keyDER)
		pendingSecret := &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{
				Namespace:   cr.Namespace,
				Name:        pendingName,
				Annotations: map[string]string{annPendingCSR: specHash, annGeneration: fmt.Sprintf("%d", cr.Generation)},
			},
			Type: corev1.SecretTypeOpaque,
			Data: map[string][]byte{
				tlsPrivateKey: pemKey,
				"csr.pem":     csrPEM,
			},
		}
		if err := controllerutil.SetControllerReference(cr, pendingSecret, r.Scheme); err != nil {
			return ctrl.Result{}, err
		}
		if err := r.Create(ctx, pendingSecret); err != nil {
			if !apierrors.IsAlreadyExists(err) {
				return ctrl.Result{}, err
			}
			// Lost a race with a parallel worker: re-fetch and let the next
			// reconcile reuse it rather than minting another key.
			return ctrl.Result{Requeue: true}, nil
		}
		pendingKey = newKey
	}

	// Reflect Issuing status before the (possibly failing) signing call.
	issueReason := certv1alpha1.ReasonPending
	if reused {
		issueReason = certv1alpha1.ReasonReused
	}
	if err := r.reflectIssuing(ctx, cr, serving, servingLeaf, specHash, eff, issueReason,
		condMsg(reused, reason), now); err != nil {
		return ctrl.Result{}, err
	}

	// ----- The signing call itself -----
	res, signErr := r.Signer.Sign(ctx, ca, eff.DNSNames, eff.Duration, pendingKey, now)

	// ##### COMMIT GATE: re-read the live CR. Any generation/spec change while
	// the signer was in flight means this receipt is stale and MUST be dropped.
	live := &certv1alpha1.Certificate{}
	if err := r.Get(ctx, req.NamespacedName, live); err != nil {
		return ctrl.Result{}, err
	}
	liveEff, liveSpecErr := live.Spec.Normalize()
	if liveSpecErr != nil || live.Generation != cr.Generation || liveEff.Hash() != specHash {
		log.Info("discarding stale issuance receipt: spec/generation changed during signing",
			"signedAtGeneration", cr.Generation, "liveGeneration", live.Generation)
		// The pending secret belongs to the old generation; remove it so the
		// newer spec starts clean, then requeue to reconcile the new config.
		_ = r.Delete(ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Namespace: cr.Namespace, Name: pendingName}})
		return ctrl.Result{Requeue: true}, nil
	}

	if signErr != nil {
		// FAILURE PATH: serving Secret is deliberately untouched.
		cur := cr.Status
		if servingValid {
			cur.NotBefore = metav1.Time{Time: servingLeaf.NotBefore}
			cur.NotAfter = metav1.Time{Time: servingLeaf.NotAfter}
			cur.Serial = pki.SerialString(servingLeaf.SerialNumber)
			cur.Thumbprint = pki.ThumbprintSHA256(servingLeaf.Raw)
			cur.SpecHash = serving.Annotations[annSpecHash]
			cur.CurrentSecret = eff.SecretName
		}
		_ = r.setReadyWithStatus(ctx, live, servingValid, certv1alpha1.ReasonIssuanceFailed,
			"issuance failed; existing valid certificate retained: "+signErr.Error(), cur)
		log.Info("issuance failed; serving secret preserved", "retainedValid", servingValid, "error", signErr.Error())
		return ctrl.Result{RequeueAfter: backoff(now)}, nil
	}
	if res == nil || res.Bundle == nil || res.Bundle.Certificate == nil {
		return ctrl.Result{}, errors.New("signer returned empty bundle")
	}
	b := res.Bundle

	// Final cryptographic acceptance check on the returned bundle.
	if err := pki.VerifyLeaf(b.Certificate, ca.Certificate, eff.DNSNames, now); err != nil {
		_ = r.setReadyWithStatus(ctx, live, servingValid, certv1alpha1.ReasonValidationFailed,
			"new bundle failed verification and was discarded: "+err.Error(), cr.Status)
		log.Info("new bundle failed verification; serving secret preserved", "error", err.Error())
		return ctrl.Result{RequeueAfter: backoff(now)}, nil
	}
	// The cert must be the public counterpart of the (possibly reused) pending key.
	if !publicKeyMatches(b.Certificate, pendingKey) {
		_ = r.setReadyWithStatus(ctx, live, servingValid, certv1alpha1.ReasonValidationFailed,
			"issued certificate public key does not match pending key; discarded", cr.Status)
		return ctrl.Result{RequeueAfter: backoff(now)}, nil
	}

	// ----- Commit: write serving Secret with conflict retries -----
	if err := r.commitSecretWithRetry(ctx, live, eff, specHash, b, ca); err != nil {
		if apierrors.IsConflict(errors.Unwrap(err)) || apierrors.IsConflict(err) {
			// Do not requeue instantly in a hot loop.
			return ctrl.Result{RequeueAfter: time.Second}, nil
		}
		return ctrl.Result{}, err
	}

	// Success: delete pending application.
	if err := r.Delete(ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Namespace: cr.Namespace, Name: pendingName}}); err != nil &&
		!apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}

	// Read the Secret back from the API and assert Secret<->status consistency.
	rb := &corev1.Secret{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: cr.Namespace, Name: eff.SecretName}, rb); err != nil {
		return ctrl.Result{}, err
	}
	readBackLeaf, err := pki.ParseCertificatePEM(rb.Data[tlsCertKey])
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("read-back tls.crt unparseable: %w", err)
	}
	if pki.ThumbprintSHA256(readBackLeaf.Raw) != pki.ThumbprintSHA256(b.Certificate.Raw) ||
		readBackLeaf.SerialNumber.Cmp(b.Certificate.SerialNumber) != 0 {
		return ctrl.Result{}, errors.New("read-back certificate does not match issued certificate")
	}
	if err := pki.VerifyLeaf(readBackLeaf, ca.Certificate, eff.DNSNames, now); err != nil {
		return ctrl.Result{}, fmt.Errorf("read-back verification failed: %w", err)
	}

	renewed := servingLeaf != nil
	rsn := certv1alpha1.ReasonIssued
	if renewed {
		rsn = certv1alpha1.ReasonRenewed
	}
	renewalAt := pki.RenewalTime(readBackLeaf.NotAfter, now, eff.RenewBefore)
	if err := r.syncStatusFromSecret(ctx, live, rb, readBackLeaf, specHash, eff, renewalAt, true,
		rsn, fmt.Sprintf("certificate issued (pendingKeyReused=%t)", reused), now); err != nil {
		return ctrl.Result{}, err
	}

	wait := time.Until(renewalAt)
	if wait > r.maxRequeue() {
		wait = r.maxRequeue()
	}
	if wait < 0 {
		wait = 0
	}
	log.Info("certificate committed",
		"serial", pki.SerialString(readBackLeaf.SerialNumber),
		"notAfter", readBackLeaf.NotAfter.UTC().Format(time.RFC3339),
		"renewalTime", renewalAt.UTC().Format(time.RFC3339),
		"reusedPendingKey", reused)
	return ctrl.Result{RequeueAfter: wait}, nil
}

// commitSecretWithRetry writes the serving Secret, retrying conflicts.
// A test hook may inject a conflicting update to prove the retry path.
func (r *Reconciler) commitSecretWithRetry(ctx context.Context, cr *certv1alpha1.Certificate,
	eff certv1alpha1.EffectiveSpec, specHash string, b *pki.IssuedBundle, ca *pki.CA) error {
	for attempt := 0; attempt < maxConflictRetries; attempt++ {
		existing := &corev1.Secret{}
		err := r.Get(ctx, types.NamespacedName{Namespace: cr.Namespace, Name: eff.SecretName}, existing)
		switch {
		case apierrors.IsNotFound(err):
			sec := r.newServingSecret(cr, eff, specHash, b, ca)
			if r.BeforeCommitSecret != nil {
				r.BeforeCommitSecret()
			}
			if createErr := r.Create(ctx, sec); createErr != nil {
				if apierrors.IsAlreadyExists(createErr) {
					continue // raced, re-fetch next iteration
				}
				return createErr
			}
			return nil
		case err != nil:
			return err
		}

		// Update an existing TLS secret in place; if someone replaced it with a
		// non-TLS secret we adopt only by converting it (still our named target).
		desired := existing.DeepCopy()
		desired.Type = corev1.SecretTypeTLS
		if desired.Data == nil {
			desired.Data = map[string][]byte{}
		}
		desired.Data[tlsCertKey] = b.CertificatePEM
		desired.Data[tlsPrivateKey] = b.PrivateKeyPEM
		desired.Data[tlsCABundleKey] = b.CAPEM
		if desired.Annotations == nil {
			desired.Annotations = map[string]string{}
		}
		applyIssuedAnnotations(desired.Annotations, specHash, cr.Generation, b)
		if err := controllerutil.SetControllerReference(cr, desired, r.Scheme); err != nil {
			return err
		}
		if r.BeforeCommitSecret != nil {
			r.BeforeCommitSecret()
		}
		if updErr := r.Update(ctx, desired); updErr != nil {
			if apierrors.IsConflict(updErr) {
				continue
			}
			return updErr
		}
		return nil
	}
	return fmt.Errorf("exceeded %d conflict retries writing secret %s/%s", maxConflictRetries, cr.Namespace, eff.SecretName)
}

func (r *Reconciler) newServingSecret(cr *certv1alpha1.Certificate, eff certv1alpha1.EffectiveSpec,
	specHash string, b *pki.IssuedBundle, ca *pki.CA) *corev1.Secret {
	sec := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Namespace:   cr.Namespace,
			Name:        eff.SecretName,
			Annotations: map[string]string{},
		},
		Type: corev1.SecretTypeTLS,
		Data: map[string][]byte{
			tlsCertKey:     b.CertificatePEM,
			tlsPrivateKey:  b.PrivateKeyPEM,
			tlsCABundleKey: b.CAPEM,
		},
	}
	applyIssuedAnnotations(sec.Annotations, specHash, cr.Generation, b)
	_ = controllerutil.SetControllerReference(cr, sec, r.Scheme)
	return sec
}

func applyIssuedAnnotations(a map[string]string, specHash string, gen int64, b *pki.IssuedBundle) {
	a[annSerial] = pki.SerialString(b.Certificate.SerialNumber)
	a[annThumbprint] = pki.ThumbprintSHA256(b.Certificate.Raw)
	a[annSpecHash] = specHash
	a[annGeneration] = fmt.Sprintf("%d", gen)
	a[annNotAfter] = b.Certificate.NotAfter.UTC().Format(time.RFC3339)
}

// loadCA reads and parses the CA Secret.
func (r *Reconciler) loadCA(ctx context.Context, namespace, name string) (*pki.CA, error) {
	sec := &corev1.Secret{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, sec); err != nil {
		return nil, fmt.Errorf("load issuer secret %s: %w", name, err)
	}
	ca, err := pki.ParseCA(sec.Data[tlsCertKey], sec.Data[tlsPrivateKey])
	if err != nil {
		return nil, fmt.Errorf("parse issuer secret %s: %w", name, err)
	}
	return ca, nil
}

func (r *Reconciler) csrMatches(csrPEM []byte, key *ecdsa.PrivateKey, dnsNames []string) bool {
	if len(csrPEM) == 0 {
		return false
	}
	block, _ := pemDecode(csrPEM)
	if block == nil {
		return false
	}
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		return false
	}
	if err := csr.CheckSignature(); err != nil {
		return false
	}
	want := map[string]bool{}
	for _, n := range dnsNames {
		want[n] = true
	}
	got := map[string]bool{}
	for _, n := range csr.DNSNames {
		got[n] = true
	}
	if len(got) != len(want) {
		return false
	}
	for n := range want {
		if !got[n] {
			return false
		}
	}
	return publicKeyMatchesFromCSR(csr, key)
}

// SetupWithManager wires watches.
func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&certv1alpha1.Certificate{}).
		Owns(&corev1.Secret{}).
		Complete(r)
}

// Backoff on persistent signing failures: 30s, capped. Kept short so tests run fast.
func backoff(now time.Time) time.Duration { return 30 * time.Second }

func condMsg(reused bool, reason string) string {
	var b strings.Builder
	if reused {
		b.WriteString("reusing pending application; ")
	}
	b.WriteString(reason)
	return b.String()
}
