package controller

import (
	"context"
	"crypto/ecdsa"
	"crypto/x509"
	"testing"
	"time"

	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	certv1alpha1 "github.com/example/cert-renewal-operator/api/v1alpha1"
	"github.com/example/cert-renewal-operator/internal/pki"
)

// ---- test doubles -----------------------------------------------------------

type mutableClock struct{ t time.Time }

func (m *mutableClock) Now() time.Time          { return m.t }
func (m *mutableClock) advance(d time.Duration) { m.t = m.t.Add(d) }
func (m *mutableClock) set(t time.Time)         { m.t = t }

// scriptedSigner scripts failures/observability for signing calls.
type scriptedSigner struct {
	failRemaining int // number of subsequent calls that fail (decremented per call)
	failErr       error
	calls         int
	onSign        func(attempt int, dns []string, pendingKey *ecdsa.PrivateKey)
	real          issuerSigner // real crypto fallback
}

type issuerSigner interface {
	signReal(ca *pki.CA, dns []string, dur time.Duration, key *ecdsa.PrivateKey, now time.Time) (*pki.IssuedBundle, error)
}

type realCAIssuer struct{}

func (realCAIssuer) signReal(ca *pki.CA, dns []string, dur time.Duration, key *ecdsa.PrivateKey, now time.Time) (*pki.IssuedBundle, error) {
	return ca.IssueWithKey(pki.IssueRequest{DNSNames: dns, Duration: dur, Now: now}, key)
}

func (s *scriptedSigner) Sign(ctx context.Context, ca *pki.CA, dns []string,
	dur time.Duration, pendingKey *ecdsa.PrivateKey, now time.Time) (*pki.IssueResult, error) {
	attempt := s.calls
	s.calls++
	if s.onSign != nil {
		s.onSign(attempt, dns, pendingKey)
	}
	if s.failRemaining > 0 {
		s.failRemaining--
		return nil, s.failErr
	}
	b, err := s.real.signReal(ca, dns, dur, pendingKey, now)
	if err != nil {
		return nil, err
	}
	return &pki.IssueResult{Bundle: b}, nil
}

// ---- harness ----------------------------------------------------------------

type harness struct {
	t        *testing.T
	ctx      context.Context
	c        client.Client
	scheme   *runtime.Scheme
	clock    *mutableClock
	signer   *scriptedSigner
	ca       *pki.CA
	ns       string
	reconcil *Reconciler
}

const caSecretName = "test-ca"

func newHarness(t *testing.T, startTime time.Time) *harness {
	t.Helper()
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{
		corev1.AddToScheme,
		certv1alpha1.AddToScheme,
	} {
		if err := add(scheme); err != nil {
			t.Fatalf("scheme registration: %v", err)
		}
	}

	caCertPEM, caKeyPEM, caCert, _, err := pki.GenerateTestCA("Local Test CA (sample only)", startTime.Add(-time.Hour), 49*time.Hour)
	if err != nil {
		t.Fatalf("generate CA: %v", err)
	}
	ca, err := pki.ParseCA(caCertPEM, caKeyPEM)
	if err != nil {
		t.Fatalf("parse CA: %v", err)
	}
	_ = caCert

	caSecret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Namespace: "default", Name: caSecretName},
		Type:       corev1.SecretTypeTLS,
		Data: map[string][]byte{
			corev1.TLSCertKey:       caCertPEM,
			corev1.TLSPrivateKeyKey: caKeyPEM,
		},
	}

	cb := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&certv1alpha1.Certificate{}).
		WithObjects(caSecret)

	cl := cb.Build()
	clk := &mutableClock{t: startTime}
	sgn := &scriptedSigner{real: realCAIssuer{}, failErr: errBoom{}}
	r := &Reconciler{
		Client:     cl,
		Scheme:     scheme,
		Clock:      clk,
		Signer:     sgn,
		Log:        logr.Discard(),
		MaxRequeue: time.Hour,
	}
	return &harness{t: t, ctx: context.Background(), c: cl, scheme: scheme, clock: clk,
		signer: sgn, ca: ca, ns: "default", reconcil: r}
}

// restart builds a fresh Reconciler over the SAME persisted storage, emulating
// a controller process restart (in-memory pending state is gone).
func (h *harness) restart() {
	sgn := &scriptedSigner{real: realCAIssuer{}, failErr: errBoom{}}
	h.signer = sgn
	h.reconcil = &Reconciler{
		Client:     h.c,
		Scheme:     h.scheme,
		Clock:      h.clock,
		Signer:     sgn,
		Log:        logr.Discard(),
		MaxRequeue: time.Hour,
	}
}

type errBoom struct{}

func (errBoom) Error() string { return "simulated CA outage: signing unavailable" }

func (h *harness) createCert(name string, spec certv1alpha1.CertificateSpec) *certv1alpha1.Certificate {
	h.t.Helper()
	cr := &certv1alpha1.Certificate{
		ObjectMeta: metav1.ObjectMeta{Namespace: h.ns, Name: name},
		Spec:       spec,
	}
	if err := h.c.Create(h.ctx, cr); err != nil {
		h.t.Fatalf("create Certificate: %v", err)
	}
	return cr
}

func (h *harness) nn(name string) types.NamespacedName {
	return types.NamespacedName{Namespace: h.ns, Name: name}
}

func (h *harness) reconcile(name string) (ctrl.Result, error) {
	h.t.Helper()
	return h.reconcil.Reconcile(h.ctx, ctrl.Request{NamespacedName: h.nn(name)})
}

func (h *harness) getCert(name string) *certv1alpha1.Certificate {
	h.t.Helper()
	cr := &certv1alpha1.Certificate{}
	if err := h.c.Get(h.ctx, h.nn(name), cr); err != nil {
		h.t.Fatalf("get cert %s: %v", name, err)
	}
	return cr
}

func (h *harness) getSecret(name string) *corev1.Secret {
	h.t.Helper()
	sec := &corev1.Secret{}
	if err := h.c.Get(h.ctx, types.NamespacedName{Namespace: h.ns, Name: name}, sec); err != nil {
		h.t.Fatalf("get secret %s: %v", name, err)
	}
	return sec
}

func (h *harness) secretMaybe(name string) (*corev1.Secret, bool) {
	sec := &corev1.Secret{}
	if err := h.c.Get(h.ctx, types.NamespacedName{Namespace: h.ns, Name: name}, sec); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, false
		}
		h.t.Fatalf("get secret %s: %v", name, err)
	}
	return sec, true
}

func leafOf(t *testing.T, sec *corev1.Secret) *x509.Certificate {
	t.Helper()
	leaf, err := pki.ParseCertificatePEM(sec.Data[corev1.TLSCertKey])
	if err != nil {
		t.Fatalf("parse serving tls.crt: %v", err)
	}
	return leaf
}

// assertConsistent verifies Secret content, serial, thumbprint and status all
// describe the SAME certificate and that the chain/SAN really verify.
func (h *harness) assertConsistent(certName string, wantDNS []string) *x509.Certificate {
	h.t.Helper()
	cr := h.getCert(certName)
	sec := h.getSecret(cr.Spec.SecretName)
	if sec.Type != corev1.SecretTypeTLS {
		h.t.Fatalf("secret %s type = %v", sec.Name, sec.Type)
	}
	if len(sec.Data["ca.crt"]) == 0 {
		h.t.Fatal("ca.crt bundle missing")
	}
	leaf := leafOf(h.t, sec)
	if err := pki.VerifyLeaf(leaf, h.ca.Certificate, wantDNS, h.clock.Now()); err != nil {
		h.t.Fatalf("chain/SAN verification: %v", err)
	}
	key, err := pki.ParsePrivateKeyPEM(sec.Data[corev1.TLSPrivateKeyKey])
	if err != nil {
		h.t.Fatalf("parse tls.key: %v", err)
	}
	gotPub, _ := x509.MarshalPKIXPublicKey(leaf.PublicKey)
	wantPub, _ := x509.MarshalPKIXPublicKey(key.Public())
	if string(gotPub) != string(wantPub) {
		h.t.Fatal("tls.crt public key does not match tls.key")
	}
	serial := pki.SerialString(leaf.SerialNumber)
	thumb := pki.ThumbprintSHA256(leaf.Raw)
	if sec.Annotations[annSerial] != serial {
		h.t.Fatalf("secret serial annotation %q != cert serial %q", sec.Annotations[annSerial], serial)
	}
	if sec.Annotations[annThumbprint] != thumb {
		h.t.Fatalf("secret thumbprint annotation mismatch")
	}
	if cr.Status.Serial != serial {
		h.t.Fatalf("status serial %q != cert serial %q", cr.Status.Serial, serial)
	}
	if cr.Status.Thumbprint != thumb {
		h.t.Fatalf("status thumbprint %q != cert %q", cr.Status.Thumbprint, thumb)
	}
	if cr.Status.SpecHash != sec.Annotations[annSpecHash] {
		h.t.Fatalf("status specHash %q != secret annotation %q", cr.Status.SpecHash, sec.Annotations[annSpecHash])
	}
	if !cr.Status.NotAfter.Time.Equal(leaf.NotAfter) {
		h.t.Fatalf("status notAfter %v != cert %v", cr.Status.NotAfter.Time, leaf.NotAfter)
	}
	if cond := condOf(cr, string(certv1alpha1.ConditionReady)); cond == nil || cond.Status != metav1.ConditionTrue {
		h.t.Fatalf("Ready not True: %+v", cond)
	}
	return leaf
}

func condOf(cr *certv1alpha1.Certificate, typ string) *metav1.Condition {
	for i := range cr.Status.Conditions {
		if cr.Status.Conditions[i].Type == typ {
			return &cr.Status.Conditions[i]
		}
	}
	return nil
}

func baseSpec(secretName string, dns ...string) certv1alpha1.CertificateSpec {
	return certv1alpha1.CertificateSpec{
		DNSNames:   dns,
		SecretName: secretName,
		Issuer:     certv1alpha1.IssuerRef{Name: caSecretName},
	}
}

func durSpec(secretName string, duration, renewBefore time.Duration, dns ...string) certv1alpha1.CertificateSpec {
	s := baseSpec(secretName, dns...)
	d := duration
	rb := renewBefore
	s.Duration = &metav1.Duration{Duration: d}
	s.RenewBefore = &metav1.Duration{Duration: rb}
	return s
}
