package controller

import (
	"context"
	"crypto/x509"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	coordinatorv1alpha1 "github.com/example/certrenewal/api/v1alpha1"
	"github.com/example/certrenewal/internal/pki"
	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// signPendingRequest finds the pending (unsigned) request of a cert and signs it.
func (f *fixture) signPendingRequest(t *testing.T, certName string) string {
	t.Helper()
	for _, cr := range f.listRequests(t) {
		if cr.Annotations[AnnotationOwnerName] != certName || isSigned(&cr) {
			continue
		}
		res, err := f.reconcileRequest(t, cr.Name)
		if err != nil {
			t.Fatalf("request reconcile: %v", err)
		}
		if res.RequeueAfter > 0 {
			t.Fatalf("expected signing to succeed, got requeue %s", res.RequeueAfter)
		}
		signed := &coordinatorv1alpha1.CertificateRequest{}
		if err := f.c.Get(context.Background(), types.NamespacedName{Namespace: testNamespace, Name: cr.Name}, signed); err != nil {
			t.Fatal(err)
		}
		return signed.Status.SerialNumber
	}
	t.Fatal("no certificate request found")
	return ""
}

func readyStatus(c *coordinatorv1alpha1.Certificate) (status, reason string) {
	for _, cond := range c.Status.Conditions {
		if cond.Type == coordinatorv1alpha1.ConditionReady {
			return string(cond.Status), cond.Reason
		}
	}
	return "", ""
}

func verifySecretChain(t *testing.T, f *fixture, s *corev1.Secret, dnsName string, now time.Time) *x509.Certificate {
	t.Helper()
	leaf, err := pki.ParseFirstCertificate(s.Data[tlsCertKey])
	if err != nil {
		t.Fatalf("parse leaf: %v", err)
	}
	caCerts, err := pki.ParseCertificates(s.Data[caCertKey])
	if err != nil {
		t.Fatal(err)
	}
	if _, err := pki.ParsePrivateKey(s.Data[tlsKeyKey]); err != nil {
		t.Fatalf("secret tls.key unparseable: %v", err)
	}
	// Independent verification: build a fresh pool from ca.crt and run the
	// real x509 path validation including hostname, EKU and validity.
	pool := x509.NewCertPool()
	pool.AddCert(caCerts[0])
	if _, err := leaf.Verify(x509.VerifyOptions{
		Roots:       pool,
		DNSName:     dnsName,
		CurrentTime: now,
		KeyUsages:   []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}); err != nil {
		t.Fatalf("certificate chain does not verify: %v", err)
	}
	// The key must match the certificate.
	signer, _ := pki.ParsePrivateKey(s.Data[tlsKeyKey])
	if !pki.PublicKeyMatches(leaf, signer.Public()) {
		t.Fatal("secret key does not match certificate public key")
	}
	return leaf
}

// --- 1. Full issuance: real CSR, real CA signature, real chain verify ------

func TestFullIssuanceRealChain(t *testing.T) {
	f := newFixture(t)
	cert := makeCertificate("test-cert", "app-tls", []string{"app.example.com", "www.example.com"}, time.Hour, 20*time.Minute)
	if err := f.c.Create(context.Background(), cert); err != nil {
		t.Fatal(err)
	}

	// First reconcile: no Secret -> pending CertificateRequest + key secret.
	res := f.reconcileCert(t, "test-cert")
	if res.RequeueAfter != 2*time.Second {
		t.Fatalf("want 2s pending requeue, got %s", res.RequeueAfter)
	}
	reqs := f.listRequests(t)
	if len(reqs) != 1 {
		t.Fatalf("want 1 certificate request, got %d", len(reqs))
	}
	if len(reqs[0].Spec.Request) == 0 {
		t.Fatal("request missing CSR PEM")
	}
	pendingKey := f.getSecret(t, reqs[0].Name+"-key")
	if len(pendingKey.Data[tlsKeyKey]) == 0 {
		t.Fatal("pending-key secret missing key")
	}

	// Request controller signs with the real CA.
	serial := f.signPendingRequest(t, "test-cert")
	if serial == "" {
		t.Fatal("empty serial after signing")
	}

	// Second reconcile: applies the receipt atomically.
	f.reconcileCert(t, "test-cert")

	s := f.getSecret(t, "app-tls")
	leaf := verifySecretChain(t, f, s, "www.example.com", f.clk.Now())

	// Secret material, serial and status summary must describe ONE cert.
	c := f.getCertificate(t, "test-cert")
	if got, reason := readyStatus(c); got != "True" || reason != coordinatorv1alpha1.ReasonReconciled {
		t.Fatalf("want Ready=True/Reconciled, got %s/%s", got, reason)
	}
	if c.Status.SerialNumber != pki.SerialHex(leaf) || c.Status.SerialNumber != serial {
		t.Fatalf("status serial %q != leaf %q / request %q", c.Status.SerialNumber, pki.SerialHex(leaf), serial)
	}
	if !c.Status.NotAfter.Time.Equal(leaf.NotAfter) || !c.Status.NotBefore.Time.Equal(leaf.NotBefore) {
		t.Fatalf("status validity %s..%s != leaf %s..%s", c.Status.NotBefore, c.Status.NotAfter, leaf.NotBefore, leaf.NotAfter)
	}
	if c.Status.ObservedGeneration != cert.Generation {
		t.Fatalf("observedGeneration=%d want %d", c.Status.ObservedGeneration, cert.Generation)
	}
	if !sets.NewString(leaf.DNSNames...).Equal(sets.NewString("app.example.com", "www.example.com")) {
		t.Fatalf("unexpected SANs: %v", leaf.DNSNames)
	}
	// Pending key secret is removed after apply.
	if err := f.c.Get(context.Background(), types.NamespacedName{Namespace: testNamespace, Name: reqs[0].Name + "-key"}, &corev1.Secret{}); !apierrors.IsNotFound(err) {
		t.Fatal("pending-key secret should be deleted")
	}
}

// --- 2. Near-expiry: renewal window is bound to the cert's own validity ----

func TestNearExpiryRenewal(t *testing.T) {
	f := newFixture(t)
	duration := 24 * time.Hour
	// Existing cert with 5 minutes of life left, otherwise perfectly valid.
	certPEM, keyPEM, caPEM := f.issueDirectly(t, "app.example.com", []string{"app.example.com"},
		baseTime.Add(-duration+5*time.Minute), baseTime.Add(5*time.Minute))
	cert := makeCertificate("test-cert", "app-tls", []string{"app.example.com"}, duration, 0)
	if err := f.c.Create(context.Background(), cert); err != nil {
		t.Fatal(err)
	}
	hash := pki.SpecFingerprint("app.example.com", []string{"app.example.com"}, duration)
	if err := f.c.Create(context.Background(), f.tlsSecret(t, "app-tls", certPEM, keyPEM, caPEM, hash)); err != nil {
		t.Fatal(err)
	}

	before := mustParseFirst(t, certPEM)
	beforeSerial := pki.SerialHex(before)

	// The cert is inside its renewal window -> a new request is created.
	f.reconcileCert(t, "test-cert")
	if len(f.listRequests(t)) != 1 {
		t.Fatal("near-expiry cert must trigger a renewal request")
	}
	c := f.getCertificate(t, "test-cert")
	if got, reason := readyStatus(c); got != "True" || reason != coordinatorv1alpha1.ReasonRenewing {
		t.Fatalf("want Ready=True/Renewing while old cert serves, got %s/%s", got, reason)
	}
	// Old certificate is still in the Secret, untouched, and still validates.
	s := f.getSecret(t, "app-tls")
	if pki.SerialHex(mustParseFirst(t, s.Data[tlsCertKey])) != beforeSerial {
		t.Fatal("old certificate was modified during pending renewal")
	}
	verifySecretChain(t, f, s, "app.example.com", f.clk.Now())

	// Sign + apply -> replaced cert has a new serial and fresh validity.
	f.signPendingRequest(t, "test-cert")
	f.reconcileCert(t, "test-cert")
	s = f.getSecret(t, "app-tls")
	after := verifySecretChain(t, f, s, "app.example.com", f.clk.Now())
	if pki.SerialHex(after) == beforeSerial {
		t.Fatal("certificate was not renewed: serial unchanged")
	}
	if !after.NotAfter.After(before.NotAfter) {
		t.Fatalf("new notAfter %s not later than old %s", after.NotAfter, before.NotAfter)
	}
	c = f.getCertificate(t, "test-cert")
	if c.Status.SerialNumber != pki.SerialHex(after) {
		t.Fatalf("status serial %q != new leaf serial %q", c.Status.SerialNumber, pki.SerialHex(after))
	}
}

// --- 3. Clock skew: the renewal decision tolerates a skewed node clock ------

func TestClockSkewRenewal(t *testing.T) {
	// 24h cert, 8h renew-before: renewal window opens at T0+16h.
	setup := func(t *testing.T, clkTime time.Time) (*fixture, string) {
		f := newFixture(t)
		certPEM, keyPEM, caPEM := f.issueDirectly(t, "app.example.com", []string{"app.example.com"},
			baseTime, baseTime.Add(24*time.Hour))
		cert := makeCertificate("test-cert", "app-tls", []string{"app.example.com"}, 24*time.Hour, 8*time.Hour)
		if err := f.c.Create(context.Background(), cert); err != nil {
			t.Fatal(err)
		}
		hash := pki.SpecFingerprint("app.example.com", []string{"app.example.com"}, 24*time.Hour)
		if err := f.c.Create(context.Background(), f.tlsSecret(t, "app-tls", certPEM, keyPEM, caPEM, hash)); err != nil {
			t.Fatal(err)
		}
		f.clk.Set(clkTime)
		f.reconcileCert(t, "test-cert")
		return f, hash
	}

	t.Run("well before window: no renewal", func(t *testing.T) {
		f, _ := setup(t, baseTime.Add(15*time.Hour))
		if len(f.listRequests(t)) != 0 {
			t.Fatal("must not renew one hour before the window opens")
		}
	})

	t.Run("20s before window: no premature renewal (skew does not shift the window)", func(t *testing.T) {
		f, _ := setup(t, baseTime.Add(16*time.Hour).Add(-20*time.Second))
		if len(f.listRequests(t)) != 0 {
			t.Fatal("a 20s skew must not pull the renewal trigger before the window")
		}
	})

	t.Run("inside window: renewal", func(t *testing.T) {
		f, _ := setup(t, baseTime.Add(17*time.Hour))
		if len(f.listRequests(t)) != 1 {
			t.Fatal("must renew inside the window")
		}
	})

	t.Run("20s before expiry: skew ahead triggers renewal", func(t *testing.T) {
		f, _ := setup(t, baseTime.Add(24*time.Hour).Add(-20*time.Second))
		if len(f.listRequests(t)) != 1 {
			t.Fatal("a clock up to 30s ahead must protect against serving an expiring cert")
		}
	})

	t.Run("clock ahead past expiry: renewal", func(t *testing.T) {
		f, _ := setup(t, baseTime.Add(25*time.Hour))
		if len(f.listRequests(t)) != 1 {
			t.Fatal("an expired cert (per skewed clock) must trigger renewal")
		}
	})
}

// --- 4. Secret update conflict: the atomic update is retried safely --------

type conflictClient struct {
	client.Client
	target   string
	failLeft int32
}

func (c *conflictClient) Update(ctx context.Context, obj client.Object, opts ...client.UpdateOption) error {
	if s, ok := obj.(*corev1.Secret); ok && s.Name == c.target && c.failLeft > 0 {
		c.failLeft--
		return apierrors.NewConflict(corev1.Resource("secrets"), s.Name, fmt.Errorf("resourceVersion conflict"))
	}
	return c.Client.Update(ctx, obj, opts...)
}

func TestSecretUpdateConflict(t *testing.T) {
	f := newFixture(t)
	duration := 24 * time.Hour
	certPEM, keyPEM, caPEM := f.issueDirectly(t, "app.example.com", []string{"app.example.com"},
		baseTime.Add(-duration+5*time.Minute), baseTime.Add(5*time.Minute))
	cert := makeCertificate("test-cert", "app-tls", []string{"app.example.com"}, duration, 0)
	if err := f.c.Create(context.Background(), cert); err != nil {
		t.Fatal(err)
	}
	hash := pki.SpecFingerprint("app.example.com", []string{"app.example.com"}, duration)
	if err := f.c.Create(context.Background(), f.tlsSecret(t, "app-tls", certPEM, keyPEM, caPEM, hash)); err != nil {
		t.Fatal(err)
	}

	f.reconcileCert(t, "test-cert")
	f.signPendingRequest(t, "test-cert")
	beforeSerial := pki.SerialHex(mustParseFirst(t, f.getSecret(t, "app-tls").Data[tlsCertKey]))

	// Intercept the first Secret update with a conflict.
	cc := &conflictClient{Client: f.c, target: "app-tls", failLeft: 1}
	r := &CertificateReconciler{Client: cc, Scheme: testScheme, CAs: f.certReconciler().CAs, Clk: f.clk}
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: testNamespace, Name: "test-cert"}}); err == nil {
		t.Fatal("conflict must surface as an error so controller-runtime requeues")
	} else if !apierrors.IsConflict(err) {
		t.Fatalf("want conflict error, got %v", err)
	}
	// Old certificate must still be exactly where it was.
	if pki.SerialHex(mustParseFirst(t, f.getSecret(t, "app-tls").Data[tlsCertKey])) != beforeSerial {
		t.Fatal("old certificate must survive the failed update")
	}
	// Requeue: same reconcile now succeeds.
	f.reconcileCert(t, "test-cert")
	after := mustParseFirst(t, f.getSecret(t, "app-tls").Data[tlsCertKey])
	if pki.SerialHex(after) == beforeSerial {
		t.Fatal("certificate should have been renewed after the conflict retry")
	}
}

// --- 5. spec change: an old receipt can never overwrite a new config --------

func TestSpecChangeOldReceiptIgnored(t *testing.T) {
	f := newFixture(t)
	cert := makeCertificate("test-cert", "app-tls", []string{"a.example.com"}, time.Hour, 20*time.Minute)
	if err := f.c.Create(context.Background(), cert); err != nil {
		t.Fatal(err)
	}
	f.reconcileCert(t, "test-cert")
	f.signPendingRequest(t, "test-cert")
	f.reconcileCert(t, "test-cert")

	oldSecret := f.getSecret(t, "app-tls")
	oldLeaf := mustParseFirst(t, oldSecret.Data[tlsCertKey])
	if !sets.NewString(oldLeaf.DNSNames...).Equal(sets.NewString("a.example.com")) {
		t.Fatalf("setup: %v", oldLeaf.DNSNames)
	}
	oldRequests := f.listRequests(t)
	if len(oldRequests) != 1 {
		t.Fatalf("setup: %d requests", len(oldRequests))
	}

	// Change the domain configuration.
	updated := f.getCertificate(t, "test-cert")
	updated.Spec.DNSNames = []string{"b.example.com"}
	updated.Spec.CommonName = "b.example.com"
	if err := f.c.Update(context.Background(), updated); err != nil {
		t.Fatal(err)
	}

	f.reconcileCert(t, "test-cert")
	reqs := f.listRequests(t)
	if len(reqs) != 2 {
		t.Fatalf("spec change must create a new request; got %d", len(reqs))
	}
	// The old signed receipt must not have touched the Secret.
	if pki.SerialHex(mustParseFirst(t, f.getSecret(t, "app-tls").Data[tlsCertKey])) != pki.SerialHex(oldLeaf) {
		t.Fatal("old-generation receipt overwrote the Secret after spec change")
	}
	// New request exists and carries the new fingerprint.
	newHash := pki.SpecFingerprint("b.example.com", []string{"b.example.com"}, time.Hour)
	var newCR *coordinatorv1alpha1.CertificateRequest
	for i := range reqs {
		if reqs[i].Annotations[AnnotationSpecHash] == newHash {
			newCR = &reqs[i]
		}
	}
	if newCR == nil {
		t.Fatal("no spec-matched request for the new domains")
	}

	// Complete the new issuance; the serving cert now carries the new domains
	// and verifies for them (and would FAIL verification for the old name).
	f.signPendingRequest(t, "test-cert")
	f.reconcileCert(t, "test-cert")
	s := f.getSecret(t, "app-tls")
	leaf := verifySecretChain(t, f, s, "b.example.com", f.clk.Now())
	if !sets.NewString(leaf.DNSNames...).Equal(sets.NewString("b.example.com")) {
		t.Fatalf("renewed cert SANs = %v", leaf.DNSNames)
	}
	pool := x509.NewCertPool()
	caCerts, _ := pki.ParseCertificates(s.Data[caCertKey])
	pool.AddCert(caCerts[0])
	if _, err := leaf.Verify(x509.VerifyOptions{Roots: pool, DNSName: "a.example.com", CurrentTime: f.clk.Now()}); err == nil {
		t.Fatal("new certificate unexpectedly verifies for the old hostname")
	}
	// The old request remains as an audit trail.
	if len(f.listRequests(t)) != 2 {
		t.Fatal("old request should be retained, not deleted")
	}
}

// Direct guard test: even if a forged request carried the new hash, a receipt
// whose leaf names the old domains is rejected before touching the Secret.
func TestApplySignedRejectsMismatchedSANs(t *testing.T) {
	f := newFixture(t)
	cert := makeCertificate("test-cert", "app-tls", []string{"a.example.com"}, time.Hour, 20*time.Minute)
	if err := f.c.Create(context.Background(), cert); err != nil {
		t.Fatal(err)
	}
	f.reconcileCert(t, "test-cert")
	f.signPendingRequest(t, "test-cert")
	f.reconcileCert(t, "test-cert")
	oldLeaf := mustParseFirst(t, f.getSecret(t, "app-tls").Data[tlsCertKey])

	// Spec changes to b; forge a signed-looking request annotated with the
	// new hash but carrying a certificate for a.
	updated := f.getCertificate(t, "test-cert")
	updated.Spec.DNSNames = []string{"b.example.com"}
	updated.Spec.CommonName = "b.example.com"
	if err := f.c.Update(context.Background(), updated); err != nil {
		t.Fatal(err)
	}
	newHash := pki.SpecFingerprint("b.example.com", []string{"b.example.com"}, time.Hour)
	forgedCertPEM, _, forgedCAPEM := f.issueDirectly(t, "a.example.com", []string{"a.example.com"},
		f.clk.Now(), f.clk.Now().Add(time.Hour))
	forged := &coordinatorv1alpha1.CertificateRequest{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: testNamespace, Name: "forged-receipt",
			Annotations: map[string]string{AnnotationOwnerName: "test-cert", AnnotationSpecHash: newHash},
		},
		Status: coordinatorv1alpha1.CertificateRequestStatus{
			Certificate: forgedCertPEM, CA: forgedCAPEM,
			Conditions: []metav1.Condition{{Type: coordinatorv1alpha1.ConditionReady, Status: metav1.ConditionTrue}},
		},
	}
	if err := f.c.Create(context.Background(), forged); err != nil {
		t.Fatal(err)
	}

	applied, err := f.certReconciler().applySigned(context.Background(), f.getCertificate(t, "test-cert"), forged, f.clk.Now())
	if err != nil {
		t.Fatal(err)
	}
	if applied {
		t.Fatal("receipt with mismatched SANs must not be applied")
	}
	if pki.SerialHex(mustParseFirst(t, f.getSecret(t, "app-tls").Data[tlsCertKey])) != pki.SerialHex(oldLeaf) {
		t.Fatal("Secret was touched by a mismatched receipt")
	}
}

// --- 6. Controller restart: persisted key material survives a restart ------

func TestControllerRestartAfterSigning(t *testing.T) {
	f := newFixture(t)
	cert := makeCertificate("test-cert", "app-tls", []string{"app.example.com"}, time.Hour, 20*time.Minute)
	if err := f.c.Create(context.Background(), cert); err != nil {
		t.Fatal(err)
	}

	// Process A: creates the request.
	f.reconcileCert(t, "test-cert")
	// Process B (brand-new reconciler structs, nothing in memory): the
	// signing happens here.
	f.signPendingRequest(t, "test-cert")
	// Process C: yet another fresh reconciler applies the receipt. The
	// private key comes from the persisted pending-key Secret.
	f.reconcileCert(t, "test-cert")

	s := f.getSecret(t, "app-tls")
	verifySecretChain(t, f, s, "app.example.com", f.clk.Now())
	c := f.getCertificate(t, "test-cert")
	if got, _ := readyStatus(c); got != "True" {
		t.Fatalf("want Ready after restart sequence, got %s", got)
	}
}

// --- 7. Issuance failure keeps the still-valid old certificate -------------

func TestIssuanceFailureKeepsOldCertificate(t *testing.T) {
	f := newFixture(t)
	duration := 24 * time.Hour
	certPEM, keyPEM, caPEM := f.issueDirectly(t, "app.example.com", []string{"app.example.com"},
		baseTime.Add(-duration+5*time.Minute), baseTime.Add(5*time.Minute))
	cert := makeCertificate("test-cert", "app-tls", []string{"app.example.com"}, duration, 0)
	if err := f.c.Create(context.Background(), cert); err != nil {
		t.Fatal(err)
	}
	hash := pki.SpecFingerprint("app.example.com", []string{"app.example.com"}, duration)
	if err := f.c.Create(context.Background(), f.tlsSecret(t, "app-tls", certPEM, keyPEM, caPEM, hash)); err != nil {
		t.Fatal(err)
	}

	// Break the CA: signing must fail.
	if err := f.c.Delete(context.Background(), &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "test-ca"}}); err != nil {
		t.Fatal(err)
	}

	f.reconcileCert(t, "test-cert") // creates pending request
	var crName string
	for _, cr := range f.listRequests(t) {
		crName = cr.Name
	}
	res, err := f.reconcileRequest(t, crName)
	if err != nil {
		t.Fatalf("CA outage should be handled with requeue, not error: %v", err)
	}
	if res.RequeueAfter <= 0 {
		t.Fatal("failed signing must request a backoff requeue")
	}

	// Certificate stays Ready/Renewing; serving Secret is untouched and still
	// validates against the original CA.
	f.reconcileCert(t, "test-cert")
	c := f.getCertificate(t, "test-cert")
	if got, reason := readyStatus(c); got != "True" || reason != coordinatorv1alpha1.ReasonRenewing {
		t.Fatalf("want Ready=True/Renewing during outage, got %s/%s", got, reason)
	}
	s := f.getSecret(t, "app-tls")
	if string(s.Data[tlsCertKey]) != string(certPEM) {
		t.Fatal("old certificate bytes must be unchanged while issuance fails")
	}
	leaf := verifySecretChain(t, f, s, "app.example.com", f.clk.Now())
	if !leaf.NotAfter.Equal(baseTime.Add(5 * time.Minute)) {
		t.Fatalf("old cert notAfter changed: %s", leaf.NotAfter)
	}

	// CA recovers: the pending request is reused and completes.
	caSecret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "test-ca"},
		Type:       corev1.SecretTypeTLS,
		Data: map[string][]byte{
			corev1.TLSCertKey:       caPEM,
			corev1.TLSPrivateKeyKey: f.caKeyPEM,
		},
	}
	if err := f.c.Create(context.Background(), caSecret); err != nil {
		t.Fatal(err)
	}
	f.reconcileRequest(t, crName)
	f.reconcileCert(t, "test-cert")
	after := mustParseFirst(t, f.getSecret(t, "app-tls").Data[tlsCertKey])
	if pki.SerialHex(after) == pki.SerialHex(leaf) {
		t.Fatal("certificate should be replaced after the CA recovers")
	}
}

// --- 8. Invalid spec is surfaced, no objects churned ------------------------

func TestInvalidSpec(t *testing.T) {
	f := newFixture(t)
	cert := makeCertificate("test-cert", "app-tls", []string{"x.example.com"}, time.Hour, time.Hour) // renewBefore == duration
	if err := f.c.Create(context.Background(), cert); err != nil {
		t.Fatal(err)
	}
	f.reconcileCert(t, "test-cert")
	c := f.getCertificate(t, "test-cert")
	if got, reason := readyStatus(c); got != "False" || reason != coordinatorv1alpha1.ReasonInvalidSpec {
		t.Fatalf("want Ready=False/InvalidSpec, got %s/%s", got, reason)
	}
	if len(f.listRequests(t)) != 0 {
		t.Fatal("invalid spec must not create requests")
	}
}

// --- 9. Private key material never reaches the logs ------------------------

type recordingSink struct {
	mu    sync.Mutex
	lines []string
}

func (s *recordingSink) Init(logr.RuntimeInfo)             {}
func (s *recordingSink) Enabled(int) bool                  { return true }
func (s *recordingSink) Info(_ int, msg string, kv ...any) { s.record(msg, kv...) }
func (s *recordingSink) Error(err error, msg string, kv ...any) {
	s.record("ERROR "+msg+" "+err.Error(), kv...)
}
func (s *recordingSink) WithValues(...any) logr.LogSink { return s }
func (s *recordingSink) WithName(string) logr.LogSink   { return s }

func (s *recordingSink) record(msg string, kv ...any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.lines = append(s.lines, fmt.Sprintf("%s %v", msg, kv))
}

func TestPrivateKeyNeverLogged(t *testing.T) {
	f := newFixture(t)
	cert := makeCertificate("test-cert", "app-tls", []string{"app.example.com"}, time.Hour, 20*time.Minute)
	if err := f.c.Create(context.Background(), cert); err != nil {
		t.Fatal(err)
	}
	sink := &recordingSink{}
	ctx := logr.NewContext(context.Background(), logr.New(sink))
	r1 := f.certReconciler()
	if _, err := r1.Reconcile(ctx, ctrl.Request{NamespacedName: types.NamespacedName{Namespace: testNamespace, Name: "test-cert"}}); err != nil {
		t.Fatal(err)
	}
	var crName string
	for _, cr := range f.listRequests(t) {
		crName = cr.Name
	}
	if _, err := f.requestReconciler().Reconcile(ctx, ctrl.Request{NamespacedName: types.NamespacedName{Namespace: testNamespace, Name: crName}}); err != nil {
		t.Fatal(err)
	}
	if _, err := r1.Reconcile(ctx, ctrl.Request{NamespacedName: types.NamespacedName{Namespace: testNamespace, Name: "test-cert"}}); err != nil {
		t.Fatal(err)
	}
	keyPEM := f.getSecret(t, "app-tls").Data[tlsKeyKey]
	if len(keyPEM) == 0 {
		t.Fatal("no key in secret")
	}
	marker := strings.TrimSpace(string(keyPEM))
	if len(marker) > 80 {
		marker = marker[:80]
	}
	for _, line := range sink.lines {
		if strings.Contains(line, marker) || strings.Contains(line, "PRIVATE KEY-----") {
			t.Fatalf("private key material appeared in a log line: %q", line)
		}
	}
}

// Regression: after issuance, a subsequent reconcile (e.g. triggered by a
// watch event, a status update, or a controller restart) must NOT delete the
// already-signed request merely because its staging key secret is gone.
func TestSteadyStateKeepsSignedRequest(t *testing.T) {
	f := newFixture(t)
	cert := makeCertificate("test-cert", "app-tls", []string{"app.example.com"}, time.Hour, 20*time.Minute)
	if err := f.c.Create(context.Background(), cert); err != nil {
		t.Fatal(err)
	}
	f.reconcileCert(t, "test-cert")
	f.signPendingRequest(t, "test-cert")
	f.reconcileCert(t, "test-cert")
	serialAfterApply := pki.SerialHex(mustParseFirst(t, f.getSecret(t, "app-tls").Data[tlsCertKey]))

	// A fresh reconciler (controller restart) reconciles the healthy object.
	f.reconcileCert(t, "test-cert")
	f.reconcileCert(t, "test-cert")

	reqs := f.listRequests(t)
	if len(reqs) != 1 {
		t.Fatalf("the signed audit request must be retained, got %d", len(reqs))
	}
	if !isSigned(&reqs[0]) {
		t.Fatal("retained request should still be signed")
	}
	if pki.SerialHex(mustParseFirst(t, f.getSecret(t, "app-tls").Data[tlsCertKey])) != serialAfterApply {
		t.Fatal("steady-state reconcile must not touch the serving secret")
	}
}

// Regression: a time-based renewal keeps the same spec hash, so renewal must
// create a fresh (differently named) request and complete; the old signed
// request remains and the secret rotates.
func TestTimeBasedRenewalAcrossReconciles(t *testing.T) {
	f := newFixture(t)
	duration := 24 * time.Hour
	certPEM, keyPEM, caPEM := f.issueDirectly(t, "app.example.com", []string{"app.example.com"},
		baseTime.Add(-duration+5*time.Minute), baseTime.Add(5*time.Minute))
	cert := makeCertificate("test-cert", "app-tls", []string{"app.example.com"}, duration, 0)
	if err := f.c.Create(context.Background(), cert); err != nil {
		t.Fatal(err)
	}
	hash := pki.SpecFingerprint("app.example.com", []string{"app.example.com"}, duration)
	if err := f.c.Create(context.Background(), f.tlsSecret(t, "app-tls", certPEM, keyPEM, caPEM, hash)); err != nil {
		t.Fatal(err)
	}
	oldSerial := pki.SerialHex(mustParseFirst(t, certPEM))

	f.reconcileCert(t, "test-cert")
	if len(f.listRequests(t)) != 1 {
		t.Fatal("renewal must create exactly one new request")
	}
	// Extra reconcile while still pending must reuse it (no duplication).
	f.reconcileCert(t, "test-cert")
	if len(f.listRequests(t)) != 1 {
		t.Fatal("pending request must be reused across reconciles")
	}

	f.signPendingRequest(t, "test-cert")
	f.reconcileCert(t, "test-cert")

	if pki.SerialHex(mustParseFirst(t, f.getSecret(t, "app-tls").Data[tlsCertKey])) == oldSerial {
		t.Fatal("serving certificate did not rotate after time-based renewal")
	}
	if len(f.listRequests(t)) != 1 {
		// exactly the new request (the pre-existing setup had none)
		t.Fatalf("expected 1 request after renewal, got %d", len(f.listRequests(t)))
	}
}

// Regression: assessSecret must bind the renewal point to the SPEC's
// renewBefore, not a fixed fraction of lifetime. With duration=10m and
// renewBefore=9m30s, the window opens 30s after the leaf's notBefore.
func TestRenewalWindowUsesSpecRenewBefore(t *testing.T) {
	f := newFixture(t)
	duration := 10 * time.Minute
	renewBefore := 9*time.Minute + 30*time.Second
	certPEM, keyPEM, caPEM := f.issueDirectly(t, "p.example.com", []string{"p.example.com"},
		baseTime, baseTime.Add(duration))
	cert := makeCertificate("test-cert", "app-tls", []string{"p.example.com"}, duration, renewBefore)
	if err := f.c.Create(context.Background(), cert); err != nil {
		t.Fatal(err)
	}
	hash := pki.SpecFingerprint("p.example.com", []string{"p.example.com"}, duration)
	s := f.tlsSecret(t, "app-tls", certPEM, keyPEM, caPEM, hash)

	a, _ := assessSecret(s, cert, renewBefore, baseTime)
	if a == nil {
		t.Fatal("secret should assess as valid")
	}
	nb := a.leaf.NotBefore
	// The renewal point is notAfter - renewBefore, i.e. 30s after notBefore
	// for a 10m cert with renewBefore 9m30s (bound to the cert's own
	// lifetime, never a wall-clock schedule).
	wantRenewal := nb.Add(duration - renewBefore)
	if !a.renewalAt.Equal(wantRenewal) {
		t.Fatalf("renewalAt = %s, want %s (notAfter - renewBefore)", a.renewalAt, wantRenewal)
	}
	// A fixed duration/3 point (notBefore+3m20s) would wrongly postpone
	// renewal by ~3 minutes — the exact bug observed in the cluster.
	if a.renewalAt.Equal(nb.Add(duration / 3)) {
		t.Fatal("renewalAt must not be derived from a fixed lifetime fraction")
	}
	// Before the window: no renewal (skew does not pull the point earlier).
	if pki.NeedsRenewal(nb, a.leaf.NotAfter, a.renewalAt, nb.Add(10*time.Second), clockSkewTolerance) {
		t.Fatal("must not renew before the renewal window")
	}
	// At/after the spec-derived point: renewal.
	if !pki.NeedsRenewal(nb, a.leaf.NotAfter, a.renewalAt, nb.Add(30*time.Second), clockSkewTolerance) {
		t.Fatal("must renew once the spec-derived window is reached")
	}
	// Within skew of expiry, still protected even though far from the window.
	if !pki.NeedsRenewal(nb, a.leaf.NotAfter, a.renewalAt, a.leaf.NotAfter.Add(-20*time.Second), clockSkewTolerance) {
		t.Fatal("must renew within skew of expiry")
	}
}
