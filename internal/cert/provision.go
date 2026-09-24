// Package cert provisions a self-signed CA and serving certificate for the
// admission webhook server, and publishes the CA bundle to the webhook
// configuration. All key generation and signing is real cryptography
// (crypto/ecdsa, crypto/x509); nothing is stubbed or hard-coded.
package cert

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"os"
	"path/filepath"
	"time"

	admissionregistrationv1 "k8s.io/api/admissionregistration/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/retry"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Default names for the webhook PKI objects.
const (
	SecretName        = "quota-reserver-webhook-cert"
	WebhookConfigName = "quota-reserver-validating-webhook"
	ServiceName       = "quota-reserver-webhook-service"
)

// Options configures EnsurePKI.
type Options struct {
	// Namespace is where the webhook Secret lives (the manager's namespace).
	Namespace string
	// ServiceName is the webhook Service used in the certificate SANs.
	ServiceName string
	// SecretName is the Secret holding ca.crt/tls.crt/tls.key.
	SecretName string
	// WebhookConfigName is the ValidatingWebhookConfiguration to patch.
	WebhookConfigName string
	// CertDir is where tls.crt/tls.key are written for the webhook server.
	CertDir string
}

// EnsurePKI makes sure a serving certificate for the webhook Service exists
// in a Secret, writes the serving pair to CertDir, and sets the CA bundle on
// every webhook of the ValidatingWebhookConfiguration. Safe to run on every
// manager start and under multiple racing replicas.
func EnsurePKI(ctx context.Context, c client.Client, opts Options) error {
	caPEM, certPEM, keyPEM, err := ensureSecret(ctx, c, opts)
	if err != nil {
		return fmt.Errorf("ensuring webhook certificate secret: %w", err)
	}
	if err := writeCertDir(opts.CertDir, certPEM, keyPEM); err != nil {
		return fmt.Errorf("writing serving certificate to %s: %w", opts.CertDir, err)
	}
	if err := ensureCABundle(ctx, c, opts.WebhookConfigName, caPEM); err != nil {
		return fmt.Errorf("patching caBundle of %s: %w", opts.WebhookConfigName, err)
	}
	return nil
}

// ensureSecret returns (caPEM, certPEM, keyPEM), generating a fresh CA and
// serving certificate if the Secret does not exist yet.
func ensureSecret(ctx context.Context, c client.Client, opts Options) (caPEM, certPEM, keyPEM []byte, err error) {
	key := types.NamespacedName{Namespace: opts.Namespace, Name: opts.SecretName}
	var sec corev1.Secret
	getErr := c.Get(ctx, key, &sec)
	if getErr == nil {
		caPEM, certPEM, keyPEM = sec.Data["ca.crt"], sec.Data["tls.crt"], sec.Data["tls.key"]
		if len(caPEM) > 0 && len(certPEM) > 0 && len(keyPEM) > 0 {
			return caPEM, certPEM, keyPEM, nil
		}
		return nil, nil, nil, fmt.Errorf("secret %s exists but is missing ca.crt/tls.crt/tls.key", key)
	}
	if !apierrors.IsNotFound(getErr) {
		return nil, nil, nil, getErr
	}

	caPEM, certPEM, keyPEM, err = Generate(opts.ServiceName, opts.Namespace)
	if err != nil {
		return nil, nil, nil, err
	}
	sec = corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Namespace: opts.Namespace, Name: opts.SecretName},
		Type:       corev1.SecretTypeTLS,
		Data: map[string][]byte{
			"ca.crt":  caPEM,
			"tls.crt": certPEM,
			"tls.key": keyPEM,
		},
	}
	if err := c.Create(ctx, &sec); err != nil && !apierrors.IsAlreadyExists(err) {
		return nil, nil, nil, err
	} else if apierrors.IsAlreadyExists(err) {
		// Another replica won the race: re-fetch and use the stored CA so all
		// replicas serve certificates signed by one shared CA.
		if err := c.Get(ctx, key, &sec); err != nil {
			return nil, nil, nil, fmt.Errorf("re-reading concurrently created secret %s: %w", key, err)
		}
	}
	caPEM, certPEM, keyPEM = sec.Data["ca.crt"], sec.Data["tls.crt"], sec.Data["tls.key"]
	if len(caPEM) == 0 || len(certPEM) == 0 || len(keyPEM) == 0 {
		return nil, nil, nil, fmt.Errorf("secret %s is missing ca.crt/tls.crt/tls.key", key)
	}
	return caPEM, certPEM, keyPEM, nil
}

// Generate creates a real ECDSA P-256 CA and a serving certificate signed by
// it, with the webhook Service DNS names as SANs. Returned as PEM blocks.
func Generate(serviceName, namespace string) (caPEM, certPEM, keyPEM []byte, err error) {
	now := time.Now()

	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("generating CA key: %w", err)
	}
	caSerial, err := randomSerial()
	if err != nil {
		return nil, nil, nil, err
	}
	caTmpl := &x509.Certificate{
		SerialNumber:          caSerial,
		Subject:               pkix.Name{CommonName: "quota-reserver-ca", Organization: []string{"quota-reserver"}},
		NotBefore:             now.Add(-time.Hour),
		NotAfter:              now.Add(10 * 365 * 24 * time.Hour),
		IsCA:                  true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, caTmpl, caTmpl, &caKey.PublicKey, caKey)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("self-signing CA certificate: %w", err)
	}
	caCert, err := x509.ParseCertificate(caDER)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("parsing CA certificate: %w", err)
	}

	srvKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("generating serving key: %w", err)
	}
	srvSerial, err := randomSerial()
	if err != nil {
		return nil, nil, nil, err
	}
	srvTmpl := &x509.Certificate{
		SerialNumber: srvSerial,
		Subject:      pkix.Name{CommonName: serviceName, Organization: []string{"quota-reserver"}},
		DNSNames: []string{
			serviceName,
			serviceName + "." + namespace,
			serviceName + "." + namespace + ".svc",
			serviceName + "." + namespace + ".svc.cluster.local",
		},
		NotBefore:   now.Add(-time.Hour),
		NotAfter:    now.Add(10 * 365 * 24 * time.Hour),
		KeyUsage:    x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	srvDER, err := x509.CreateCertificate(rand.Reader, srvTmpl, caCert, &srvKey.PublicKey, caKey)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("signing serving certificate: %w", err)
	}

	srvKeyDER, err := x509.MarshalECPrivateKey(srvKey)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("encoding serving key: %w", err)
	}

	caPEM = pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER})
	certPEM = pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srvDER})
	keyPEM = pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: srvKeyDER})
	return caPEM, certPEM, keyPEM, nil
}

func randomSerial() (*big.Int, error) {
	limit := new(big.Int).Lsh(big.NewInt(1), 128)
	serial, err := rand.Int(rand.Reader, limit)
	if err != nil {
		return nil, fmt.Errorf("generating certificate serial: %w", err)
	}
	return serial, nil
}

// writeCertDir writes tls.crt and tls.key where the webhook server reads them.
func writeCertDir(dir string, certPEM, keyPEM []byte) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(dir, "tls.crt"), certPEM, 0o644); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, "tls.key"), keyPEM, 0o600)
}

// ensureCABundle sets the CA bundle on every webhook entry, retrying on
// resourceVersion conflicts.
func ensureCABundle(ctx context.Context, c client.Client, name string, caPEM []byte) error {
	return retry.RetryOnConflict(retry.DefaultBackoff, func() error {
		var cfg admissionregistrationv1.ValidatingWebhookConfiguration
		if err := c.Get(ctx, types.NamespacedName{Name: name}, &cfg); err != nil {
			return err
		}
		changed := false
		for i := range cfg.Webhooks {
			if !bytes.Equal(cfg.Webhooks[i].ClientConfig.CABundle, caPEM) {
				cfg.Webhooks[i].ClientConfig.CABundle = caPEM
				changed = true
			}
		}
		if !changed {
			return nil
		}
		return c.Update(ctx, &cfg)
	})
}
