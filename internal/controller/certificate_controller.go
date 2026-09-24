package controller

import (
	"context"
	"crypto/ecdsa"
	"crypto/x509"
	"errors"
	"fmt"
	"time"

	coordinatorv1alpha1 "github.com/example/certrenewal/api/v1alpha1"
	"github.com/example/certrenewal/internal/ca"
	"github.com/example/certrenewal/internal/clock"
	"github.com/example/certrenewal/internal/pki"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/rand"
	"k8s.io/apimachinery/pkg/util/sets"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"
)

// CertificateReconciler keeps the TLS Secret of each Certificate in sync with
// its spec and inside its validity/renewal window.
type CertificateReconciler struct {
	client.Client
	Scheme *runtime.Scheme
	CAs    ca.Source
	Clk    clock.Clock
}

// LabelPendingKey marks the Secret that temporarily holds the private key of
// an in-flight CertificateRequest. Persisting the key in the cluster (instead
// of process memory) is what makes controller restarts safe: after a restart
// the reconciler re-reads the key and can still apply the signed receipt.
const LabelPendingKey = "coordinator.example.com/pending-key"

// Reconcile is idempotent and restart-safe: every decision is derived from
// the live Secret + CertificateRequest state, never from in-memory caches.
func (r *CertificateReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)
	now := r.Clk.Now()

	cert := &coordinatorv1alpha1.Certificate{}
	if err := r.Get(ctx, req.NamespacedName, cert); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	duration, renewBefore, specErr := normalizeSpec(cert.Spec)
	if specErr != nil {
		cert.Status.ObservedGeneration = cert.Generation
		setCondition(&cert.Status.Conditions, cert.Generation, coordinatorv1alpha1.ConditionReady,
			"False", coordinatorv1alpha1.ReasonInvalidSpec, specErr.Error())
		_ = r.Status().Update(ctx, cert)
		return ctrl.Result{}, nil
	}
	specHash := pki.SpecFingerprint(cert.Spec.CommonName, cert.Spec.DNSNames, duration)

	// Best-effort hygiene: drop pending-key secrets that belong to an older
	// spec or a vanished request. Never touches the serving TLS Secret.
	r.cleanupOrphanKeySecrets(ctx, cert, specHash)

	// --- 1. Assess the certificate currently in the Secret -----------------
	secret := &corev1.Secret{}
	secretErr := r.Get(ctx, types.NamespacedName{Namespace: cert.Namespace, Name: cert.Spec.SecretName}, secret)

	var current *assessed
	switch {
	case secretErr == nil:
		current, _ = assessSecret(secret, cert, renewBefore, now)
	case apierrors.IsNotFound(secretErr):
		secret = nil
	default:
		return ctrl.Result{}, secretErr
	}

	needNew := current == nil
	if current != nil {
		if current.specHash != specHash {
			logger.Info("spec changed; renewal required", "oldHash", current.specHash, "newHash", specHash)
			needNew = true
		} else if pki.NeedsRenewal(current.leaf.NotBefore, current.leaf.NotAfter, current.renewalAt, now, clockSkewTolerance) {
			logger.Info("certificate in renewal window", "notAfter", current.leaf.NotAfter, "renewalAt", current.renewalAt)
			needNew = true
		}
	}

	// --- 2. Find a reusable, spec-matched CertificateRequest ---------------
	// Requests whose fingerprint belongs to an older spec are ignored: an old
	// receipt can never overwrite the new domain configuration.
	crq, err := r.findReusableRequest(ctx, cert, specHash)
	if err != nil {
		return ctrl.Result{}, err
	}

	if crq != nil && isSigned(crq) {
		if current != nil && pki.SerialHex(current.leaf) == crq.Status.SerialNumber {
			// This receipt is already the serving certificate: steady state.
			// Its staging key is long gone and the request is retained as an
			// audit record.
			crq = nil
		} else {
			applied, err := r.applySigned(ctx, cert, crq, now)
			if err != nil {
				return ctrl.Result{}, err
			}
			if applied {
				// Re-read the Secret so the status summary describes exactly the
				// material just written.
				fresh := &corev1.Secret{}
				if err := r.Get(ctx, types.NamespacedName{Namespace: cert.Namespace, Name: cert.Spec.SecretName}, fresh); err == nil {
					secret = fresh
				}
				return r.finishStatus(ctx, cert, duration, renewBefore, secret, crq, coordinatorv1alpha1.ReasonReconciled, false)
			}
			// Signed payload failed verification: never written to the Secret.
			// Delete the request so a fresh one is created next round.
			logger.Info("discarding signed request that failed verification", "request", crq.Name)
			if err := r.deleteRequestAndKey(ctx, crq); err != nil {
				return ctrl.Result{}, err
			}
			crq = nil
			needNew = true
		}
	}

	if crq != nil {
		// Unsigned candidate: it is reusable only while its staging key is
		// present. Missing key means the request can never be applied.
		if _, err := r.getPendingKey(ctx, crq); err != nil {
			if !apierrors.IsNotFound(err) {
				return ctrl.Result{}, err
			}
			logger.Info("deleting certificate request whose key material is missing", "request", crq.Name)
			if err := r.deleteRequestAndKey(ctx, crq); err != nil {
				return ctrl.Result{}, err
			}
			crq = nil
		}
	}

	// --- 3. Issuance path: the old certificate must survive failures -------
	if needNew {
		if crq == nil {
			created, err := r.createRequest(ctx, cert, duration, specHash)
			if err != nil {
				return ctrl.Result{}, err
			}
			crq = created
		}
		// A pending, spec-matched request now exists. If the Secret still
		// contains a currently-valid certificate, keep serving it and report
		// Renewing; the Secret is never deleted before the replacement is
		// verified. If nothing valid exists, report Issuing.
		if current != nil && now.Before(current.leaf.NotAfter) {
			return r.finishStatus(ctx, cert, duration, renewBefore, secret, crq, coordinatorv1alpha1.ReasonRenewing, true)
		}
		return r.finishStatus(ctx, cert, duration, renewBefore, secret, crq, coordinatorv1alpha1.ReasonIssuing, true)
	}

	// --- 4. Existing Secret is good: report observed state ----------------
	return r.finishStatus(ctx, cert, duration, renewBefore, secret, crq, coordinatorv1alpha1.ReasonReconciled, false)
}

type assessed struct {
	leaf      *x509.Certificate
	caCert    *x509.Certificate
	privKey   *ecdsa.PrivateKey
	specHash  string
	renewalAt time.Time
}

// assessSecret validates tls.key/tls.crt/ca.crt: parses material, checks the
// key pair, verifies the chain against ca.crt with real x509 path validation,
// and compares SANs/CN against the live spec. Returns nil with a description
// when anything is wrong.
func assessSecret(secret *corev1.Secret, cert *coordinatorv1alpha1.Certificate, renewBefore time.Duration, now time.Time) (*assessed, string) {
	certPEM := secret.Data[tlsCertKey]
	if len(certPEM) == 0 {
		return nil, "missing tls.crt"
	}
	keyPEM := secret.Data[tlsKeyKey]
	if len(keyPEM) == 0 {
		return nil, "missing tls.key"
	}
	caPEM := secret.Data[caCertKey]
	if len(caPEM) == 0 {
		return nil, "missing ca.crt"
	}
	signer, err := pki.ParsePrivateKey(keyPEM)
	if err != nil {
		return nil, "bad tls.key: " + err.Error()
	}
	leaf, err := pki.ParseFirstCertificate(certPEM)
	if err != nil {
		return nil, "bad tls.crt: " + err.Error()
	}
	caCerts, err := pki.ParseCertificates(caPEM)
	if err != nil {
		return nil, "bad ca.crt: " + err.Error()
	}
	ecKey, ok := signer.(*ecdsa.PrivateKey)
	if !ok {
		return nil, "tls.key is not an ECDSA key"
	}
	if !pki.PublicKeyMatches(leaf, &ecKey.PublicKey) {
		return nil, "tls.key does not match tls.crt public key"
	}
	want := sets.NewString(cert.Spec.DNSNames...)
	got := sets.NewString(leaf.DNSNames...)
	if !want.Equal(got) {
		return nil, fmt.Sprintf("SAN drift: secret has %v, spec wants %v", got.List(), want.List())
	}
	if cert.Spec.CommonName != leaf.Subject.CommonName {
		return nil, fmt.Sprintf("CN drift: secret has %q, spec wants %q", leaf.Subject.CommonName, cert.Spec.CommonName)
	}
	if err := pki.VerifyChain(leaf, caCerts[0], cert.Spec.DNSNames[0], now); err != nil {
		return nil, "chain verification failed: " + err.Error()
	}
	h := ""
	if secret.Annotations != nil {
		h = secret.Annotations[AnnotationSpecHash]
	}
	return &assessed{
		leaf: leaf, caCert: caCerts[0], privKey: ecKey,
		specHash:  h,
		renewalAt: pki.RenewalTime(leaf.NotBefore, leaf.NotAfter, renewBefore),
	}, ""
}

// findReusableRequest returns the newest CertificateRequest owned by cert
// whose spec fingerprint equals specHash. An unsigned request wins (it is the
// in-flight application this or a previous reconcile created); otherwise the
// newest signed receipt is returned (it may already be the serving cert).
func (r *CertificateReconciler) findReusableRequest(ctx context.Context, cert *coordinatorv1alpha1.Certificate, specHash string) (*coordinatorv1alpha1.CertificateRequest, error) {
	list := &coordinatorv1alpha1.CertificateRequestList{}
	if err := r.List(ctx, list, client.InNamespace(cert.Namespace)); err != nil {
		return nil, err
	}
	var pending, signed *coordinatorv1alpha1.CertificateRequest
	for i := range list.Items {
		cr := &list.Items[i]
		if cr.Annotations[AnnotationOwnerName] != cert.Name {
			continue
		}
		if cr.Annotations[AnnotationSpecHash] != specHash {
			continue // receipt for a previous domain/duration configuration
		}
		newer := func(cur *coordinatorv1alpha1.CertificateRequest) bool {
			return cur == nil || cr.CreationTimestamp.After(cur.CreationTimestamp.Time)
		}
		if isSigned(cr) {
			if newer(signed) {
				signed = cr
			}
		} else if newer(pending) {
			pending = cr
		}
	}
	if pending != nil {
		return pending, nil
	}
	return signed, nil
}

func isSigned(cr *coordinatorv1alpha1.CertificateRequest) bool {
	if cr == nil {
		return false
	}
	c, ok := readyCondition(cr.Status.Conditions)
	return ok && c.Status == "True" && len(cr.Status.Certificate) > 0
}

// requestName carries a random suffix: time-based renewals keep the same spec
// hash, so a deterministic name would collide with the retained signed request
// and deadlock renewal. Repeated reconciles reuse the in-flight request via
// findReusableRequest, not by name.
func requestName(cert *coordinatorv1alpha1.Certificate, specHash string) string {
	return fmt.Sprintf("%s-%s-%s", cert.Name, specHash[:12], rand.String(5))
}

// deleteRequestAndKey removes a request together with its staging key secret.
func (r *CertificateReconciler) deleteRequestAndKey(ctx context.Context, crq *coordinatorv1alpha1.CertificateRequest) error {
	keySecret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Namespace: crq.Namespace, Name: pendingKeySecretName(crq)}}
	if err := r.Delete(ctx, keySecret); err != nil && !apierrors.IsNotFound(err) {
		return fmt.Errorf("delete staging key: %w", err)
	}
	if err := r.Delete(ctx, crq); err != nil && !apierrors.IsNotFound(err) {
		return err
	}
	return nil
}

// pendingKeySecretName names the Secret holding the in-flight private key.
func pendingKeySecretName(crq *coordinatorv1alpha1.CertificateRequest) string {
	return crq.Name + "-key"
}

// createRequest generates a fresh key + CSR, persists the key in a dedicated
// pending-key Secret (restart-safe), and creates the CertificateRequest. The
// private key is never logged.
func (r *CertificateReconciler) createRequest(ctx context.Context, cert *coordinatorv1alpha1.Certificate, duration time.Duration, specHash string) (*coordinatorv1alpha1.CertificateRequest, error) {
	csrPEM, key, err := pki.CreateCSR(pki.CSRParams{
		CommonName: cert.Spec.CommonName,
		DNSNames:   cert.Spec.DNSNames,
	})
	if err != nil {
		return nil, err
	}
	keyPEM, err := pki.EncodePrivateKey(key)
	if err != nil {
		return nil, err
	}
	cr := &coordinatorv1alpha1.CertificateRequest{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: cert.Namespace,
			Name:      requestName(cert, specHash),
			Annotations: map[string]string{
				AnnotationOwnerName: cert.Name,
				AnnotationSpecHash:  specHash,
			},
			Labels: map[string]string{
				LabelOwnerName: cert.Name,
				LabelSpecHash:  specHash,
			},
		},
		Spec: coordinatorv1alpha1.CertificateRequestSpec{
			Request:   csrPEM,
			Duration:  metav1.Duration{Duration: duration},
			IssuerRef: cert.Spec.IssuerRef,
		},
	}
	keySecret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: cert.Namespace,
			Name:      cr.Name + "-key",
			Labels: map[string]string{
				LabelOwnerName:  cert.Name,
				LabelSpecHash:   specHash,
				LabelPendingKey: "true",
			},
		},
		// Opaque, not kubernetes.io/tls: this staging object holds only the
		// pending private key; the API server requires both tls.crt and
		// tls.key for TLS-typed secrets.
		Type: corev1.SecretTypeOpaque,
		Data: map[string][]byte{tlsKeyKey: keyPEM},
	}
	if err := r.Create(ctx, keySecret); err != nil && !apierrors.IsAlreadyExists(err) {
		return nil, fmt.Errorf("store pending key: %w", err)
	}
	if err := r.Create(ctx, cr); err != nil {
		if apierrors.IsAlreadyExists(err) {
			// A previous reconcile (or the previous controller process)
			// created it first: reuse the in-flight application instead of
			// generating a second CSR.
			got := &coordinatorv1alpha1.CertificateRequest{}
			if gErr := r.Get(ctx, types.NamespacedName{Namespace: cr.Namespace, Name: cr.Name}, got); gErr != nil {
				return nil, gErr
			}
			return got, nil
		}
		return nil, err
	}
	return cr, nil
}

// getPendingKey reads the private key PEM belonging to a CertificateRequest.
func (r *CertificateReconciler) getPendingKey(ctx context.Context, crq *coordinatorv1alpha1.CertificateRequest) ([]byte, error) {
	secret := &corev1.Secret{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: crq.Namespace, Name: pendingKeySecretName(crq)}, secret); err != nil {
		return nil, err
	}
	keyPEM := secret.Data[tlsKeyKey]
	if len(keyPEM) == 0 {
		return nil, apierrors.NewNotFound(corev1.Resource("secret"), pendingKeySecretName(crq))
	}
	return keyPEM, nil
}

// cleanupOrphanKeySecrets deletes pending-key Secrets of this Certificate
// whose spec hash is stale or whose CertificateRequest no longer exists.
// Failures are logged and ignored: cleanup never blocks issuance.
func (r *CertificateReconciler) cleanupOrphanKeySecrets(ctx context.Context, cert *coordinatorv1alpha1.Certificate, specHash string) {
	logger := log.FromContext(ctx)
	list := &corev1.SecretList{}
	if err := r.List(ctx, list,
		client.InNamespace(cert.Namespace),
		client.MatchingLabels{LabelOwnerName: cert.Name, LabelPendingKey: "true"},
	); err != nil {
		return
	}
	for i := range list.Items {
		s := &list.Items[i]
		stale := s.Labels[LabelSpecHash] != specHash
		if !stale {
			crq := &coordinatorv1alpha1.CertificateRequest{}
			err := r.Get(ctx, types.NamespacedName{Namespace: s.Namespace, Name: s.Name[:len(s.Name)-len("-key")]}, crq)
			stale = apierrors.IsNotFound(err)
		}
		if stale {
			if err := r.Delete(ctx, s); err != nil && !apierrors.IsNotFound(err) {
				logger.Info("failed to clean orphan pending-key secret", "secret", s.Name, "error", err.Error())
			}
		}
	}
}

// applySigned writes the signed certificate (plus its pending private key)
// into the TLS Secret. The full chain is verified locally before any write,
// and the Secret is updated atomically (never deleted first). Returns false
// when the receipt cannot be applied (caller then creates a fresh request).
func (r *CertificateReconciler) applySigned(ctx context.Context, cert *coordinatorv1alpha1.Certificate, crq *coordinatorv1alpha1.CertificateRequest, now time.Time) (bool, error) {
	logger := log.FromContext(ctx)

	leaf, err := pki.ParseFirstCertificate(crq.Status.Certificate)
	if err != nil {
		return false, nil
	}
	caCerts, err := pki.ParseCertificates(crq.Status.CA)
	if err != nil {
		return false, nil
	}
	if err := pki.VerifyChain(leaf, caCerts[0], cert.Spec.DNSNames[0], now); err != nil {
		logger.Info("signed receipt failed chain verification; not writing secret", "error", err.Error())
		return false, nil
	}
	// SANs in the receipt must match the LIVE spec, not the request's old
	// claims — an old-generation receipt can never overwrite a new domain
	// configuration.
	if !sameStrSet(leaf.DNSNames, cert.Spec.DNSNames) || leaf.Subject.CommonName != cert.Spec.CommonName {
		logger.Info("signed receipt does not match live spec; ignoring", "leafSANs", leaf.DNSNames)
		return false, nil
	}

	keyPEM, err := r.getPendingKey(ctx, crq)
	if err != nil {
		if apierrors.IsNotFound(err) {
			return false, nil
		}
		return false, err
	}
	signer, err := pki.ParsePrivateKey(keyPEM)
	if err != nil {
		return false, err
	}
	if !pki.PublicKeyMatches(leaf, signer.Public()) {
		return false, errors.New("signed certificate public key does not match pending private key")
	}

	secret := &corev1.Secret{}
	errGet := r.Get(ctx, types.NamespacedName{Namespace: cert.Namespace, Name: cert.Spec.SecretName}, secret)
	switch {
	case apierrors.IsNotFound(errGet):
		secret = &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{
				Namespace:   cert.Namespace,
				Name:        cert.Spec.SecretName,
				Labels:      map[string]string{LabelOwnerName: cert.Name},
				Annotations: map[string]string{AnnotationSpecHash: crq.Annotations[AnnotationSpecHash]},
			},
			Type: corev1.SecretTypeTLS,
			Data: map[string][]byte{
				tlsCertKey: crq.Status.Certificate,
				caCertKey:  crq.Status.CA,
				tlsKeyKey:  keyPEM,
			},
		}
		if err := r.Create(ctx, secret); err != nil && !apierrors.IsAlreadyExists(err) {
			return false, err
		}
	case errGet != nil:
		return false, errGet
	default:
		// Atomic in-place update; the old (still valid) certificate is only
		// replaced after the new chain verified, and never deleted first.
		updated := secret.DeepCopy()
		if updated.Labels == nil {
			updated.Labels = map[string]string{}
		}
		updated.Labels[LabelOwnerName] = cert.Name
		if updated.Annotations == nil {
			updated.Annotations = map[string]string{}
		}
		updated.Annotations[AnnotationSpecHash] = crq.Annotations[AnnotationSpecHash]
		updated.Type = corev1.SecretTypeTLS
		updated.Data = map[string][]byte{
			tlsCertKey: crq.Status.Certificate,
			caCertKey:  crq.Status.CA,
			tlsKeyKey:  keyPEM,
		}
		if err := r.Update(ctx, updated); err != nil {
			return false, err
		}
	}
	// Key material now lives in the serving Secret; drop the staging copy.
	keySecret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Namespace: crq.Namespace, Name: pendingKeySecretName(crq)}}
	if err := r.Delete(ctx, keySecret); err != nil && !apierrors.IsNotFound(err) {
		logger.Info("failed to delete pending-key secret after apply", "error", err.Error())
	}
	return true, nil
}

// finishStatus updates the Certificate status so Secret material, serial
// number and the status summary always describe the same certificate.
func (r *CertificateReconciler) finishStatus(
	ctx context.Context, cert *coordinatorv1alpha1.Certificate,
	duration, renewBefore time.Duration,
	secret *corev1.Secret, crq *coordinatorv1alpha1.CertificateRequest,
	reason string, pending bool,
) (ctrl.Result, error) {
	gen := cert.Generation
	cert.Status.ObservedGeneration = gen
	cert.Status.SpecHash = pki.SpecFingerprint(cert.Spec.CommonName, cert.Spec.DNSNames, duration)

	res := ctrl.Result{}
	if pending {
		res.RequeueAfter = 2 * time.Second
	}

	if secret != nil {
		if leaf, err := pki.ParseFirstCertificate(secret.Data[tlsCertKey]); err == nil {
			cert.Status.SerialNumber = pki.SerialHex(leaf)
			cert.Status.NotBefore = ptrTime(leaf.NotBefore)
			cert.Status.NotAfter = ptrTime(leaf.NotAfter)
			cert.Status.RenewalTime = ptrTime(pki.RenewalTime(leaf.NotBefore, leaf.NotAfter, renewBefore))
			if !pending {
				// Healthy: wake at the renewal point (bound to the cert's
				// own validity). Skew tolerance is reserved for the expiry
				// decision, so the wake is not pulled earlier. A 5s floor
				// covers clock granularity and guarantees periodic
				// reconciliation even if the math yields ~0.
				until := time.Until(cert.Status.RenewalTime.Time)
				if until < 5*time.Second {
					until = 5 * time.Second
				}
				res = ctrl.Result{RequeueAfter: until}
			}
		}
	}

	switch reason {
	case coordinatorv1alpha1.ReasonReconciled:
		setCondition(&cert.Status.Conditions, gen, coordinatorv1alpha1.ConditionReady, "True",
			reason, fmt.Sprintf("current; serial %s", cert.Status.SerialNumber))
	case coordinatorv1alpha1.ReasonRenewing:
		// Old cert still valid; remain Ready while renewal is in flight.
		notAfter := ""
		if cert.Status.NotAfter != nil {
			notAfter = cert.Status.NotAfter.Time.Format(time.RFC3339)
		}
		setCondition(&cert.Status.Conditions, gen, coordinatorv1alpha1.ConditionReady, "True",
			reason, fmt.Sprintf("renewal in flight; current cert remains valid until %s", notAfter))
	case coordinatorv1alpha1.ReasonIssuing:
		msg := "initial issuance in flight"
		if crq != nil {
			if c, ok := readyCondition(crq.Status.Conditions); ok && c.Status == "False" {
				msg = "issuance pending: " + c.Message
			}
		}
		setCondition(&cert.Status.Conditions, gen, coordinatorv1alpha1.ConditionReady, "False",
			coordinatorv1alpha1.ReasonIssuanceFailed, msg)
	default:
		setCondition(&cert.Status.Conditions, gen, coordinatorv1alpha1.ConditionReady, "False", reason, "")
	}

	if err := r.Status().Update(ctx, cert); err != nil {
		// Status conflict: requeue; nothing is lost because every decision is
		// derived from Secret/CR objects.
		return ctrl.Result{Requeue: true}, nil
	}
	return res, nil
}

func sameStrSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	seen := map[string]int{}
	for _, s := range a {
		seen[s]++
	}
	for _, s := range b {
		seen[s]--
		if seen[s] < 0 {
			return false
		}
	}
	for _, v := range seen {
		if v != 0 {
			return false
		}
	}
	return true
}

// SetupWithManager wires watches. Secret changes enqueue the owning
// Certificate (external writers / update conflicts are detected immediately).
func (r *CertificateReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&coordinatorv1alpha1.Certificate{}).
		Watches(
			&coordinatorv1alpha1.CertificateRequest{},
			enqueueByRequestAnnotation(),
		).
		Watches(
			&corev1.Secret{},
			enqueueOwningCertificate(),
			builder.WithPredicates(secretHasOwnerPredicate()),
		).
		Complete(r)
}
