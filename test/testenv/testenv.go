// Package testenv spins up envtest, generates a real self-signed CA and
// server certificate with crypto/x509 + RSA, starts the conversion and
// admission webhook servers over TLS, and installs the Timer CRD wired to
// them. All cryptographic material is generated per test run - nothing is
// checked in.
package testenv

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/example/crd-migration-demo/internal/admission"
	"github.com/example/crd-migration-demo/internal/conversion"
	admissionregv1 "k8s.io/api/admissionregistration/v1"
	apiextv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	apiextclient "k8s.io/apiextensions-apiserver/pkg/client/clientset/clientset"
	apiextscheme "k8s.io/apiextensions-apiserver/pkg/client/clientset/clientset/scheme"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	ctrllog "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
)

const (
	crdName = "timers.timer.example.com"
	crdFile = "../../config/crd/bases/timer.example.com_timers.yaml"
)

// Env bundles a running envtest control plane and the webhook servers.
type Env struct {
	EnvTest      *envtest.Environment
	Cfg          *rest.Config
	KubeClient   kubernetes.Interface
	APIExtClient apiextclient.Interface
	WebhookURL   string // https://host:port base
	ConversionH  *conversion.Handler

	caPEM     []byte
	serverURL *url.URL
}

// Start boots everything. storageVersion must be "v1" or "v1alpha1".
func Start(t *testing.T, storageVersion string) *Env {
	t.Helper()
	ctrllog.SetLogger(zap.New(zap.WriteTo(&testWriter{t}), zap.UseDevMode(false)))
	logger := ctrllog.Log

	assets := os.Getenv("KUBEBUILDER_ASSETS")
	if assets == "" {
		t.Fatalf("KUBEBUILDER_ASSETS is not set; run 'make test' (it runs setup-envtest) or export it manually")
	}

	crd := loadCRD(t, storageVersion)
	te := &envtest.Environment{
		BinaryAssetsDirectory: assets,
		CRDs:                  []*apiextv1.CustomResourceDefinition{crd},
	}

	restCfg, err := te.Start()
	if err != nil {
		t.Fatalf("starting envtest: %v", err)
	}
	t.Cleanup(func() { _ = te.Stop() })

	kc, err := kubernetes.NewForConfig(restCfg)
	if err != nil {
		t.Fatalf("kubernetes client: %v", err)
	}
	extc, err := apiextclient.NewForConfig(restCfg)
	if err != nil {
		t.Fatalf("apiext client: %v", err)
	}

	// --- Real CA + server certificate -----------------------------------
	caCert, caKey, caPEM := mustCA(t)
	srvCertPEM, srvKeyPEM := mustServerCert(t, caCert, caKey)

	// --- Webhook HTTP+TLS server ----------------------------------------
	convH := conversion.NewHandler(logger.WithName("conversion"))
	admH := admission.NewHandler(logger.WithName("admission"))
	mux := http.NewServeMux()
	mux.Handle("/convert", convH)
	mux.Handle("/mutate-v1-timer", admH)
	mux.Handle("/validate-v1-timer", admH)

	port := freePort(t)
	srv := &http.Server{
		Addr:              fmt.Sprintf("127.0.0.1:%d", port),
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	tlsCertPath := filepath.Join(t.TempDir(), "tls.crt")
	tlsKeyPath := filepath.Join(t.TempDir(), "tls.key")
	mustWrite(t, tlsCertPath, srvCertPEM)
	mustWrite(t, tlsKeyPath, srvKeyPEM)

	go func() {
		if err := srv.ListenAndServeTLS(tlsCertPath, tlsKeyPath); err != nil && err != http.ErrServerClosed {
			t.Logf("webhook server error: %v", err)
		}
	}()
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
	})

	baseURL := fmt.Sprintf("https://127.0.0.1:%d", port)
	if err := waitReady(baseURL+"/convert", caPEM, 10*time.Second); err != nil {
		t.Fatalf("webhook server not ready: %v", err)
	}

	u, err := url.Parse(baseURL)
	if err != nil {
		t.Fatalf("parse webhook url: %v", err)
	}
	e := &Env{
		EnvTest:      te,
		Cfg:          restCfg,
		KubeClient:   kc,
		APIExtClient: extc,
		WebhookURL:   baseURL,
		ConversionH:  convH,
		caPEM:        caPEM,
		serverURL:    u,
	}

	// Point the CRD conversion webhook and admission registrations at us.
	e.patchCRDConversion(t)
	e.installAdmissionRegistrations(t)
	return e
}

// SetStorageVersion flips which CRD version is stored. It updates the CRD
// spec (only one version may carry storage:true) and re-points the
// conversion webhook at the local server.
func (e *Env) SetStorageVersion(t *testing.T, storageVersion string) {
	t.Helper()
	crd := loadCRD(t, storageVersion)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	existing, err := e.APIExtClient.ApiextensionsV1().CustomResourceDefinitions().Get(ctx, crdName, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get CRD: %v", err)
	}
	crd.ResourceVersion = existing.ResourceVersion
	// Preserve the server-asserved storedVersions: envtest/etcd still holds
	// whatever objects existed under the old storage version.
	crd.Status = existing.Status
	if _, err := e.APIExtClient.ApiextensionsV1().CustomResourceDefinitions().
		Update(ctx, crd, metav1.UpdateOptions{}); err != nil {
		t.Fatalf("update CRD storage version: %v", err)
	}
	e.patchCRDConversion(t)
}

// loadCRD reads the shipped manifest and forces one version to be storage.
func loadCRD(t *testing.T, storageVersion string) *apiextv1.CustomResourceDefinition {
	t.Helper()
	raw, err := os.ReadFile(crdFile)
	if err != nil {
		t.Fatalf("reading CRD manifest: %v", err)
	}
	obj, _, err := apiextscheme.Codecs.UniversalDeserializer().Decode(raw, nil, nil)
	if err != nil {
		t.Fatalf("decoding CRD yaml: %v", err)
	}
	crd, ok := obj.(*apiextv1.CustomResourceDefinition)
	if !ok {
		t.Fatalf("decoded %T, expected CustomResourceDefinition", obj)
	}
	for i := range crd.Spec.Versions {
		crd.Spec.Versions[i].Storage = crd.Spec.Versions[i].Name == storageVersion
	}
	return crd
}

func (e *Env) patchCRDConversion(t *testing.T) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	crd, err := e.APIExtClient.ApiextensionsV1().CustomResourceDefinitions().Get(ctx, crdName, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("get CRD: %v", err)
	}
	convertURL := e.serverURL.String() + "/convert"
	if crd.Spec.Conversion == nil {
		crd.Spec.Conversion = &apiextv1.CustomResourceConversion{}
	}
	if crd.Spec.Conversion.Webhook == nil {
		crd.Spec.Conversion.Webhook = &apiextv1.WebhookConversion{
			ConversionReviewVersions: []string{"v1"},
		}
	}
	crd.Spec.Conversion.Strategy = apiextv1.WebhookConverter
	crd.Spec.Conversion.Webhook.ClientConfig = &apiextv1.WebhookClientConfig{
		URL:      strPtr(convertURL),
		CABundle: e.caPEM,
	}
	if _, err := e.APIExtClient.ApiextensionsV1().CustomResourceDefinitions().
		Update(ctx, crd, metav1.UpdateOptions{}); err != nil {
		t.Fatalf("patch CRD conversion webhook: %v", err)
	}
}

func (e *Env) installAdmissionRegistrations(t *testing.T) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	mutURL := e.serverURL.String() + "/mutate-v1-timer"
	valURL := e.serverURL.String() + "/validate-v1-timer"
	side := admissionregv1.SideEffectClassNone
	timeout := int32(10)
	fail := admissionregv1.Fail
	allScopes := admissionregv1.AllScopes
	rule := admissionregv1.RuleWithOperations{
		Operations: []admissionregv1.OperationType{
			admissionregv1.Create, admissionregv1.Update,
		},
		Rule: admissionregv1.Rule{
			APIGroups:   []string{"timer.example.com"},
			APIVersions: []string{"v1", "v1alpha1"},
			Resources:   []string{"timers"},
			Scope:       &allScopes,
		},
	}

	_ = e.KubeClient.AdmissionregistrationV1().MutatingWebhookConfigurations().
		Delete(ctx, "timer-mutating-webhook", metav1.DeleteOptions{})
	mut := &admissionregv1.MutatingWebhookConfiguration{
		ObjectMeta: metav1.ObjectMeta{Name: "timer-mutating-webhook"},
		Webhooks: []admissionregv1.MutatingWebhook{{
			Name:                    "mutate.timer.example.com",
			AdmissionReviewVersions: []string{"v1"},
			SideEffects:             &side,
			TimeoutSeconds:          &timeout,
			ClientConfig:            admissionregv1.WebhookClientConfig{URL: &mutURL, CABundle: e.caPEM},
			Rules:                   []admissionregv1.RuleWithOperations{rule},
		}},
	}
	if _, err := e.KubeClient.AdmissionregistrationV1().MutatingWebhookConfigurations().
		Create(ctx, mut, metav1.CreateOptions{}); err != nil {
		t.Fatalf("create mutating webhook config: %v", err)
	}

	_ = e.KubeClient.AdmissionregistrationV1().ValidatingWebhookConfigurations().
		Delete(ctx, "timer-validating-webhook", metav1.DeleteOptions{})
	val := &admissionregv1.ValidatingWebhookConfiguration{
		ObjectMeta: metav1.ObjectMeta{Name: "timer-validating-webhook"},
		Webhooks: []admissionregv1.ValidatingWebhook{{
			Name:                    "validate.timer.example.com",
			AdmissionReviewVersions: []string{"v1"},
			SideEffects:             &side,
			TimeoutSeconds:          &timeout,
			FailurePolicy:           &fail,
			ClientConfig:            admissionregv1.WebhookClientConfig{URL: &valURL, CABundle: e.caPEM},
			Rules:                   []admissionregv1.RuleWithOperations{rule},
		}},
	}
	if _, err := e.KubeClient.AdmissionregistrationV1().ValidatingWebhookConfigurations().
		Create(ctx, val, metav1.CreateOptions{}); err != nil {
		t.Fatalf("create validating webhook config: %v", err)
	}
}

// --- crypto ----------------------------------------------------------------

// mustCA generates a fresh RSA-2048 CA key and self-signed CA certificate.
func mustCA(t *testing.T) (*x509.Certificate, *rsa.PrivateKey, []byte) {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generating CA RSA key: %v", err)
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		t.Fatalf("CA serial: %v", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: "timer-test-ca"},
		NotBefore:             time.Now().Add(-time.Minute),
		NotAfter:              time.Now().Add(24 * time.Hour),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("creating CA certificate: %v", err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatalf("parsing CA certificate: %v", err)
	}
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	return cert, key, pemBytes
}

// mustServerCert issues a server (leaf) certificate signed by the CA, valid
// for 127.0.0.1 and localhost.
func mustServerCert(t *testing.T, caCert *x509.Certificate, caKey *rsa.PrivateKey) ([]byte, []byte) {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generating server RSA key: %v", err)
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		t.Fatalf("server serial: %v", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: serial,
		Subject:      pkix.Name{CommonName: "timer-webhook"},
		NotBefore:    time.Now().Add(-time.Minute),
		NotAfter:     time.Now().Add(24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		DNSNames:     []string{"timer-webhook", "localhost"},
		IPAddresses:  []net.IP{net.ParseIP("127.0.0.1")},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, caCert, &key.PublicKey, caKey)
	if err != nil {
		t.Fatalf("creating server certificate: %v", err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM := pem.EncodeToMemory(&pem.Block{
		Type:  "RSA PRIVATE KEY",
		Bytes: x509.MarshalPKCS1PrivateKey(key),
	})
	return certPEM, keyPEM
}

func mustWrite(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatalf("writing %s: %v", path, err)
	}
}

func strPtr(s string) *string { return &s }
