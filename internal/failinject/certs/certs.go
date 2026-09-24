// Package certs generates a self-signed CA and a server certificate signed by
// it. All crypto operations are real (RSA-2048 / ECDSA-P256 + x509); nothing
// here is mocked. The manager calls Ensure at startup so the admission
// webhook works on a fresh kind cluster with no external certificate tooling.
package certs

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"time"
)

// File names controller-runtime's webhook server expects.
const (
	ServerCertName = "tls.crt"
	ServerKeyName  = "tls.key"
	CACertName     = "ca.crt"
)

// EnsureCerts makes sure certDir contains tls.crt, tls.key and ca.crt. If all
// three already exist they are reused; otherwise a fresh CA and server
// certificate are generated and written with 0600/0644 permissions.
//
// dnsNames must include the Service DNS name the apiserver dials, e.g.
// config-distributor-webhook.config-system.svc.
func EnsureCerts(certDir string, dnsNames []string, ips []net.IP) error {
	if hasAll(certDir) {
		return nil
	}
	if err := os.MkdirAll(certDir, 0o755); err != nil {
		return fmt.Errorf("create cert dir %s: %w", certDir, err)
	}
	caCert, caKey, caPEM, err := newCA()
	if err != nil {
		return err
	}
	certPEM, keyPEM, err := newSignedServer(caCert, caKey, dnsNames, ips)
	if err != nil {
		return err
	}
	if err := writeFile(filepath.Join(certDir, ServerCertName), certPEM, 0o644); err != nil {
		return err
	}
	if err := writeFile(filepath.Join(certDir, ServerKeyName), keyPEM, 0o600); err != nil {
		return err
	}
	if err := writeFile(filepath.Join(certDir, CACertName), caPEM, 0o644); err != nil {
		return err
	}
	return nil
}

// LoadCAPEM returns the PEM-encoded CA bundle used to register the webhook.
func LoadCAPEM(certDir string) ([]byte, error) {
	return os.ReadFile(filepath.Join(certDir, CACertName))
}

func hasAll(dir string) bool {
	for _, name := range []string{ServerCertName, ServerKeyName, CACertName} {
		if _, err := os.Stat(filepath.Join(dir, name)); err != nil {
			return false
		}
	}
	return true
}

func writeFile(path string, data []byte, mode os.FileMode) error {
	if err := os.WriteFile(path, data, mode); err != nil {
		return fmt.Errorf("write %s: %w", path, err)
	}
	return nil
}

func newCA() (*x509.Certificate, *ecdsa.PrivateKey, []byte, error) {
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("generate CA key: %w", err)
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		return nil, nil, nil, err
	}
	tmpl := &x509.Certificate{
		SerialNumber: serial,
		Subject: pkix.Name{
			CommonName:   "config-distributor-ca",
			Organization: []string{"config-distributor"},
		},
		NotBefore:             time.Now().Add(-time.Minute),
		NotAfter:              time.Now().Add(10 * 365 * 24 * time.Hour),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &caKey.PublicKey, caKey)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("create CA cert: %w", err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		return nil, nil, nil, err
	}
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	return cert, caKey, pemBytes, nil
}

func newSignedServer(caCert *x509.Certificate, caKey *ecdsa.PrivateKey,
	dnsNames []string, ips []net.IP) ([]byte, []byte, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, fmt.Errorf("generate server key: %w", err)
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		return nil, nil, err
	}
	tmpl := &x509.Certificate{
		SerialNumber: serial,
		Subject: pkix.Name{
			CommonName:   dnsNames[0],
			Organization: []string{"config-distributor"},
		},
		NotBefore:   time.Now().Add(-time.Minute),
		NotAfter:    time.Now().Add(5 * 365 * 24 * time.Hour),
		KeyUsage:    x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		DNSNames:    dnsNames,
		IPAddresses: ips,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, caCert, &key.PublicKey, caKey)
	if err != nil {
		return nil, nil, fmt.Errorf("sign server cert: %w", err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return nil, nil, err
	}
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})
	return certPEM, keyPEM, nil
}
