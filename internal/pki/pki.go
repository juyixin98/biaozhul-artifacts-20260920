// Package pki implements the real cryptographic operations used by the
// renewal coordinator: key/CSR generation, CA signing with crypto/x509,
// certificate/chain parsing and verification, renewal-window math and the
// spec fingerprint.
//
// Nothing here is mocked: all signatures are produced and verified with the
// standard library. Private keys are never returned to loggers.
package pki

import (
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"sort"
	"strings"
	"time"
)

// Defaults used when the Certificate spec omits the duration.
const (
	DefaultDuration  = 24 * time.Hour
	MinDuration      = 10 * time.Minute
	DefaultRenewFrac = 3 // renew before 1/3 of lifetime
)

// PEM block types.
const (
	PEMCertificate   = "CERTIFICATE"
	PEMRSAPrivateKey = "RSA PRIVATE KEY"
	PEMECPrivateKey  = "EC PRIVATE KEY"
	PKCS8PrivateKey  = "PRIVATE KEY"
)

// GeneratePrivateKey creates a fresh P-256 ECDSA private key.
func GeneratePrivateKey() (*ecdsa.PrivateKey, error) {
	return ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
}

// EncodePrivateKey PEM-encodes a private key (PKCS#8 so it works for any
// algorithm; the sample CA uses EC and RSA keys).
func EncodePrivateKey(key crypto.Signer) ([]byte, error) {
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return nil, fmt.Errorf("marshal private key: %w", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: PKCS8PrivateKey, Bytes: der}), nil
}

// ParsePrivateKey decodes a PEM private key (PKCS#1, PKCS#8 or EC SEC1).
func ParsePrivateKey(pemBytes []byte) (crypto.Signer, error) {
	block, _ := pem.Decode(pemBytes)
	if block == nil {
		return nil, errors.New("no PEM block found in private key")
	}
	switch block.Type {
	case PKCS8PrivateKey:
		k, err := x509.ParsePKCS8PrivateKey(block.Bytes)
		if err != nil {
			return nil, fmt.Errorf("parse pkcs8 key: %w", err)
		}
		signer, ok := k.(crypto.Signer)
		if !ok {
			return nil, errors.New("pkcs8 key is not a signer")
		}
		return signer, nil
	case PEMRSAPrivateKey:
		k, err := x509.ParsePKCS1PrivateKey(block.Bytes)
		if err != nil {
			return nil, fmt.Errorf("parse rsa key: %w", err)
		}
		return k, nil
	case PEMECPrivateKey:
		k, err := x509.ParseECPrivateKey(block.Bytes)
		if err != nil {
			return nil, fmt.Errorf("parse ec key: %w", err)
		}
		return k, nil
	default:
		return nil, fmt.Errorf("unsupported private key PEM type %q", block.Type)
	}
}

// ParseCertificates decodes every CERTIFICATE PEM block.
func ParseCertificates(pemBytes []byte) ([]*x509.Certificate, error) {
	var certs []*x509.Certificate
	rest := pemBytes
	for {
		var block *pem.Block
		block, rest = pem.Decode(rest)
		if block == nil {
			break
		}
		if block.Type != PEMCertificate {
			continue
		}
		cert, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			return nil, fmt.Errorf("parse certificate: %w", err)
		}
		certs = append(certs, cert)
	}
	if len(certs) == 0 {
		return nil, errors.New("no certificates found in PEM data")
	}
	return certs, nil
}

// ParseFirstCertificate decodes and returns the first certificate block.
func ParseFirstCertificate(pemBytes []byte) (*x509.Certificate, error) {
	certs, err := ParseCertificates(pemBytes)
	if err != nil {
		return nil, err
	}
	return certs[0], nil
}

// GenerateTestCA creates a fresh self-signed test CA.
//
// WARNING: this exists for samples and tests only. It is not a production CA
// and its key material is generated on the fly for ephemeral clusters.
func GenerateTestCA(now time.Time, lifetime time.Duration) (certPEM, keyPEM []byte, err error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, fmt.Errorf("generate CA key: %w", err)
	}
	serial, err := randSerial()
	if err != nil {
		return nil, nil, err
	}
	tmpl := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               subject("Test CA", nil),
		NotBefore:             now.Add(-1 * time.Minute),
		NotAfter:              now.Add(lifetime),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
		IsCA:                  true,
		MaxPathLen:            1,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		return nil, nil, fmt.Errorf("create CA certificate: %w", err)
	}
	certPEM = pem.EncodeToMemory(&pem.Block{Type: PEMCertificate, Bytes: der})
	keyPEM, err = EncodePrivateKey(key)
	if err != nil {
		return nil, nil, err
	}
	return certPEM, keyPEM, nil
}

// CSRParams describes the requested leaf certificate.
type CSRParams struct {
	CommonName string
	DNSNames   []string
}

// CreateCSR generates a fresh private key and a PEM-encoded PKCS#10 CSR.
// The key is returned to the caller for storage in the TLS Secret; it is not
// logged.
func CreateCSR(p CSRParams) (csrPEM []byte, key *ecdsa.PrivateKey, err error) {
	key, err = GeneratePrivateKey()
	if err != nil {
		return nil, nil, err
	}
	tmpl := x509.CertificateRequest{
		Subject:  subject(p.CommonName, p.DNSNames),
		DNSNames: dedupeSorted(p.DNSNames),
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, &tmpl, key)
	if err != nil {
		return nil, nil, fmt.Errorf("create CSR: %w", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der}), key, nil
}

// SignResult is the output of SignCSR.
type SignResult struct {
	CertificatePEM []byte
	CAPEM          []byte
	Certificate    *x509.Certificate
}

// SignCSR verifies a CSR against the expected parameters and signs it with the
// CA, performing real x509 operations end to end.
//
// The CSR signature and requestor key are verified (x509.ParseCertificateRequest
// + CheckSignature), and the requested DNS names must match wantDNS exactly
// (set comparison) so an old-generation CSR cannot obtain a certificate for a
// new domain configuration.
func SignCSR(caCert *x509.Certificate, caKey crypto.Signer, csrPEM []byte, wantCommonName string, wantDNS []string, notBefore, notAfter time.Time) (*SignResult, error) {
	block, _ := pem.Decode(csrPEM)
	if block == nil {
		return nil, errors.New("CSR is not PEM encoded")
	}
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("parse CSR: %w", err)
	}
	if err := csr.CheckSignature(); err != nil {
		return nil, fmt.Errorf("verify CSR signature: %w", err)
	}
	if wantCommonName != csr.Subject.CommonName {
		return nil, fmt.Errorf("CSR common name mismatch: got %q want %q", csr.Subject.CommonName, wantCommonName)
	}
	if !sameStringSet(csr.DNSNames, wantDNS) {
		return nil, fmt.Errorf("CSR SAN mismatch: got %v want %v", csr.DNSNames, wantDNS)
	}
	if !notAfter.After(notBefore) {
		return nil, fmt.Errorf("invalid validity window: notBefore %s notAfter %s", notBefore, notAfter)
	}
	serial, err := randSerial()
	if err != nil {
		return nil, err
	}
	tmpl := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               csr.Subject,
		DNSNames:              csr.DNSNames,
		NotBefore:             notBefore,
		NotAfter:              notAfter,
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth},
		BasicConstraintsValid: true,
		IsCA:                  false,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, caCert, csr.PublicKey, caKey)
	if err != nil {
		return nil, fmt.Errorf("sign certificate: %w", err)
	}
	leaf, err := x509.ParseCertificate(der)
	if err != nil {
		return nil, fmt.Errorf("re-parse signed certificate: %w", err)
	}
	caPEM := pem.EncodeToMemory(&pem.Block{Type: PEMCertificate, Bytes: caCert.Raw})
	return &SignResult{
		CertificatePEM: pem.EncodeToMemory(&pem.Block{Type: PEMCertificate, Bytes: der}),
		CAPEM:          caPEM,
		Certificate:    leaf,
	}, nil
}

// VerifyChain validates leaf against caCert for serverAuth with the given name
// at time now. It builds a fresh pool, so this is a genuine path
// verification (signature, issuer, validity, key usage, EKU, hostname).
func VerifyChain(leaf, caCert *x509.Certificate, dnsName string, now time.Time) error {
	if leaf == nil || caCert == nil {
		return errors.New("nil certificate")
	}
	pool := x509.NewCertPool()
	pool.AddCert(caCert)
	_, err := leaf.Verify(x509.VerifyOptions{
		Roots:       pool,
		DNSName:     dnsName,
		CurrentTime: now,
		KeyUsages:   []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	})
	return err
}

// PublicKeyMatches reports whether cert's public key equals pub, comparing
// the PKIX encodings so it works for any supported algorithm.
func PublicKeyMatches(cert *x509.Certificate, pub crypto.PublicKey) bool {
	a, err := x509.MarshalPKIXPublicKey(cert.PublicKey)
	if err != nil {
		return false
	}
	b, err := x509.MarshalPKIXPublicKey(pub)
	if err != nil {
		return false
	}
	return string(a) == string(b)
}

// SerialHex returns the ":"-separated hex serial used in status and events,// matching the text form of x509.Certificate.SerialNumber.String() semantics
// ("01:ab:...").
func SerialHex(cert *x509.Certificate) string {
	b := cert.SerialNumber.Bytes()
	if len(b) == 0 {
		return "00"
	}
	parts := make([]string, len(b))
	for i, byt := range b {
		parts[i] = fmt.Sprintf("%02x", byt)
	}
	return strings.Join(parts, ":")
}

// RenewalTime computes when the certificate enters its renewal window.
//
// The window is bound to the certificate's own validity period:
//
//	renewal = notAfter - renewBefore, clamped to notBefore.
//
// A renewal is due when now (plus skew tolerance) is at or past renewal, or
// when the cert has expired.
func RenewalTime(notBefore, notAfter time.Time, renewBefore time.Duration) time.Time {
	r := notAfter.Add(-renewBefore)
	if r.Before(notBefore) {
		r = notBefore
	}
	return r
}

// NeedsRenewal reports whether the certificate is (or is within skew of
// being) expired, or has reached its renewal window.
//
// Skew tolerance is applied only to the expiry decision — it protects a
// node whose clock runs ahead from serving a cert that is in fact expired.
// It is deliberately NOT applied to the renewal point: pulling the renewal
// trigger earlier by the skew amount would let a freshly issued certificate
// whose renewBefore is close to its lifetime renew immediately in a tight
// loop. Near-expiry still renews promptly via the expiry branch.
func NeedsRenewal(notBefore, notAfter, renewal time.Time, now time.Time, skewTolerance time.Duration) bool {
	if !now.Add(skewTolerance).Before(notAfter) {
		return true // expired (or within skew of expiry)
	}
	return !now.Before(renewal)
}

// SpecFingerprint is the stable hash of the signing inputs derived from a
// Certificate spec. A change to any domain or the duration changes the
// fingerprint and forces a new CertificateRequest.
func SpecFingerprint(commonName string, dnsNames []string, duration time.Duration) string {
	dns := make([]string, len(dnsNames))
	copy(dns, dnsNames)
	sort.Strings(dns)
	h := sha256.Sum256([]byte(fmt.Sprintf("v1\ncn=%s\ndns=%s\ndur=%s\n", commonName, strings.Join(dns, ","), duration.String())))
	return hex.EncodeToString(h[:16])
}

// randSerial returns a 128-bit positive serial from crypto/rand.
func randSerial() (*big.Int, error) {
	return rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
}
