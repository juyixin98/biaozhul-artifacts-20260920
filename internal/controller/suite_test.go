package controller

import (
	"context"
	"crypto"
	"crypto/x509"
	"testing"
	"time"

	coordinatorv1alpha1 "github.com/example/certrenewal/api/v1alpha1"
	"github.com/example/certrenewal/internal/ca"
	"github.com/example/certrenewal/internal/clock"
	"github.com/example/certrenewal/internal/pki"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const testNamespace = "default"

var testScheme = runtime.NewScheme()

func init() {
	_ = clientgoscheme.AddToScheme(testScheme)
	_ = coordinatorv1alpha1.AddToScheme(testScheme)
}

// fixture bundles everything a single test needs. Each test gets fresh
// reconcilers (simulating a controller restart when desired) over the same
// persisted objects.
type fixture struct {
	clk      *clock.Fake
	c        client.Client
	caCert   *x509.Certificate
	caKey    crypto.Signer
	caKeyPEM []byte
}

var baseTime = time.Date(2026, 9, 23, 12, 0, 0, 0, time.UTC)

func newFixture(t *testing.T, objs ...client.Object) *fixture {
	t.Helper()
	caPEM, caKeyPEM, err := pki.GenerateTestCA(baseTime, 30*24*time.Hour)
	if err != nil {
		t.Fatalf("generate test CA: %v", err)
	}
	caCerts, err := pki.ParseCertificates(caPEM)
	if err != nil {
		t.Fatal(err)
	}
	caKey, err := pki.ParsePrivateKey(caKeyPEM)
	if err != nil {
		t.Fatal(err)
	}
	caSecret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "test-ca"},
		Type:       corev1.SecretTypeTLS,
		Data: map[string][]byte{
			corev1.TLSCertKey:       caPEM,
			corev1.TLSPrivateKeyKey: caKeyPEM,
		},
	}
	c := fake.NewClientBuilder().
		WithScheme(testScheme).
		WithObjects(append(objs, caSecret)...).
		WithStatusSubresource(&coordinatorv1alpha1.Certificate{}, &coordinatorv1alpha1.CertificateRequest{}).
		Build()
	return &fixture{
		clk:      &clock.Fake{T: baseTime},
		c:        c,
		caCert:   caCerts[0],
		caKey:    caKey,
		caKeyPEM: caKeyPEM,
	}
}

func (f *fixture) certReconciler() *CertificateReconciler {
	return &CertificateReconciler{
		Client: f.c,
		Scheme: testScheme,
		CAs:    ca.NewSecretSource(f.c),
		Clk:    f.clk,
	}
}

func (f *fixture) requestReconciler() *CertificateRequestReconciler {
	return &CertificateRequestReconciler{
		Client: f.c,
		Scheme: testScheme,
		CAs:    ca.NewSecretSource(f.c),
		Signer: &SignWithCA{Client: f.c, Clk: f.clk},
		Clk:    f.clk,
	}
}

func (f *fixture) reconcileCert(t *testing.T, name string) ctrl.Result {
	t.Helper()
	res, err := f.certReconciler().Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: testNamespace, Name: name}})
	if err != nil {
		t.Fatalf("certificate reconcile: %v", err)
	}
	return res
}

func (f *fixture) reconcileRequest(t *testing.T, name string) (ctrl.Result, error) {
	t.Helper()
	return f.requestReconciler().Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: testNamespace, Name: name}})
}

func makeCertificate(name, secretName string, dnsNames []string, duration, renewBefore time.Duration) *coordinatorv1alpha1.Certificate {
	spec := coordinatorv1alpha1.CertificateSpec{
		DNSNames:   dnsNames,
		CommonName: dnsNames[0],
		SecretName: secretName,
		IssuerRef:  coordinatorv1alpha1.IssuerRef{Name: "test-ca"},
	}
	if duration > 0 {
		spec.Duration = &metav1.Duration{Duration: duration}
	}
	if renewBefore > 0 {
		spec.RenewBefore = &metav1.Duration{Duration: renewBefore}
	}
	return &coordinatorv1alpha1.Certificate{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: name},
		Spec:       spec,
	}
}

// issueDirectly signs a leaf with the test CA for arbitrary validity bounds
// (used to build "nearly expired" starting states).
func (f *fixture) issueDirectly(t *testing.T, cn string, dnsNames []string, notBefore, notAfter time.Time) (certPEM, keyPEM, caPEM []byte) {
	t.Helper()
	csrPEM, key, err := pki.CreateCSR(pki.CSRParams{CommonName: cn, DNSNames: dnsNames})
	if err != nil {
		t.Fatal(err)
	}
	res, err := pki.SignCSR(f.caCert, f.caKey, csrPEM, cn, dnsNames, notBefore, notAfter)
	if err != nil {
		t.Fatal(err)
	}
	keyPEM, err = pki.EncodePrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	return res.CertificatePEM, keyPEM, res.CAPEM
}

func (f *fixture) tlsSecret(t *testing.T, name string, certPEM, keyPEM, caPEM []byte, specHash string) *corev1.Secret {
	t.Helper()
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Namespace:   testNamespace,
			Name:        name,
			Labels:      map[string]string{LabelOwnerName: "test-cert"},
			Annotations: map[string]string{AnnotationSpecHash: specHash},
		},
		Type: corev1.SecretTypeTLS,
		Data: map[string][]byte{
			tlsCertKey: certPEM,
			tlsKeyKey:  keyPEM,
			caCertKey:  caPEM,
		},
	}
}

func (f *fixture) getCertificate(t *testing.T, name string) *coordinatorv1alpha1.Certificate {
	t.Helper()
	c := &coordinatorv1alpha1.Certificate{}
	if err := f.c.Get(context.Background(), types.NamespacedName{Namespace: testNamespace, Name: name}, c); err != nil {
		t.Fatal(err)
	}
	return c
}

func (f *fixture) getSecret(t *testing.T, name string) *corev1.Secret {
	t.Helper()
	s := &corev1.Secret{}
	if err := f.c.Get(context.Background(), types.NamespacedName{Namespace: testNamespace, Name: name}, s); err != nil {
		t.Fatal(err)
	}
	return s
}

func (f *fixture) listRequests(t *testing.T) []coordinatorv1alpha1.CertificateRequest {
	t.Helper()
	list := &coordinatorv1alpha1.CertificateRequestList{}
	if err := f.c.List(context.Background(), list, client.InNamespace(testNamespace)); err != nil {
		t.Fatal(err)
	}
	return list.Items
}

func mustParseFirst(t *testing.T, pemBytes []byte) *x509.Certificate {
	t.Helper()
	cert, err := pki.ParseFirstCertificate(pemBytes)
	if err != nil {
		t.Fatal(err)
	}
	return cert
}
