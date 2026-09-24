// Package pki wraps real crypto/x509 operations for the local test CA.
//
// The CA here is intentionally simple and is ONLY meant as a local sample:
// it signs leaf certificates with an ECDSA P-256 key. No private key
// material is ever logged by callers.
package pki

import (
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"sort"
	"strings"
	"time"
)

const (
	// MaxSkew is tolerated wall-clock skew between controller and CA clocks.
	// Renewal is forced this early in addition to renewBefore, and a freshly
	// issued cert whose NotBefore lies within +/- MaxSkew of now is accepted.
	MaxSkew = 5 * time.Minute
)

// IssueResult is what a Signer hands back after a (possibly slow) signing call.
type IssueResult struct {
	Bundle *IssuedBundle
}

// CA bundles a loaded certificate authority.
type CA struct {
	Certificate *x509.Certificate
	Signer      crypto.Signer
	RawCert     []byte
}

// ParseCA decodes tls.crt / tls.key PEM bytes from a Secret.
func ParseCA(certPEM, keyPEM []byte) (*CA, error) {
	leaf, err := ParseCertificatePEM(certPEM)
	if err != nil {
		return nil, fmt.Errorf("parse ca certificate: %w", err)
	}
	key, err := ParsePrivateKeyPEM(keyPEM)
	if err != nil {
		return nil, err
	}
	if !leaf.IsCA {
		return nil, errors.New("referenced certificate is not a CA (basicConstraints CA:TRUE missing)")
	}
	// Sanity: public key in the cert must match the private key.
	if !publicKeysEqual(leaf.PublicKey, key.Public()) {
		return nil, errors.New("ca certificate public key does not match tls.key")
	}
	return &CA{Certificate: leaf, Signer: key, RawCert: leaf.Raw}, nil
}

// IssueRequest is a validated issuance request.
type IssueRequest struct {
	DNSNames []string
	Duration time.Duration
	// Now is injected for tests; defaults to time.Now.
	Now time.Time
	// Serial optionally pins a serial (tests only). Nil => random.
	Serial *big.Int
}

// IssuedBundle is the result of a successful issuance.
type IssuedBundle struct {
	CertificatePEM []byte
	PrivateKeyPEM  []byte
	CAPEM          []byte
	Certificate    *x509.Certificate
	PrivateKey     *ecdsa.PrivateKey
}

// Issue performs a real key generation, CSR, and CA signature.
// It verifies the resulting chain before returning.
func (ca *CA) Issue(req IssueRequest) (*IssuedBundle, error) {
	return ca.issueWithKey(req, nil)
}

// IssueWithKey behaves like Issue but reuses an externally supplied leaf key
// (the persisted key of an in-flight application), so retries/restarts do not
// rotate the key while a renewal is pending.
func (ca *CA) IssueWithKey(req IssueRequest, leafKey *ecdsa.PrivateKey) (*IssuedBundle, error) {
	return ca.issueWithKey(req, leafKey)
}

func (ca *CA) issueWithKey(req IssueRequest, suppliedKey *ecdsa.PrivateKey) (*IssuedBundle, error) {
	if len(req.DNSNames) == 0 {
		return nil, errors.New("dnsNames must not be empty")
	}
	if req.Duration <= 0 {
		return nil, errors.New("duration must be positive")
	}
	now := req.Now
	if now.IsZero() {
		now = time.Now()
	}
	if now.Add(req.Duration).After(ca.Certificate.NotAfter) {
		return nil, fmt.Errorf("requested validity extends beyond CA expiry (%v > %v); rotate the test CA first",
			now.Add(req.Duration).UTC().Format(time.RFC3339), ca.Certificate.NotAfter.UTC().Format(time.RFC3339))
	}

	// 1. Fresh leaf key (ECDSA P-256) per renewal — unless resuming a
	// persisted pending application, whose key must be reused.
	leafKey := suppliedKey
	if leafKey == nil {
		k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if err != nil {
			return nil, fmt.Errorf("generate leaf key: %w", err)
		}
		leafKey = k
	}

	// 2. Real x509 template signed by the CA.
	tmpl := &x509.Certificate{
		SerialNumber:          newSerial(req.Serial),
		Subject:               pkixName(req.DNSNames[0]),
		NotBefore:             now.Add(-MaxSkew),
		NotAfter:              now.Add(req.Duration),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth},
		DNSNames:              append([]string(nil), req.DNSNames...),
		BasicConstraintsValid: true,
		IsCA:                  false,
	}

	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca.Certificate, &leafKey.PublicKey, ca.Signer)
	if err != nil {
		return nil, fmt.Errorf("create certificate: %w", err)
	}
	leaf, err := x509.ParseCertificate(der)
	if err != nil {
		return nil, fmt.Errorf("re-parse issued certificate: %w", err)
	}

	// 3. Verify signature, chain, SAN and validity immediately — never hand
	// back something the controller could persist that would fail later.
	if err := VerifyLeaf(leaf, ca.Certificate, req.DNSNames, now); err != nil {
		return nil, fmt.Errorf("post-issuance verification: %w", err)
	}

	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyDER, err := x509.MarshalPKCS8PrivateKey(leafKey)
	if err != nil {
		return nil, fmt.Errorf("marshal leaf key: %w", err)
	}
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER})
	caPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: ca.Certificate.Raw})

	return &IssuedBundle{
		CertificatePEM: certPEM,
		PrivateKeyPEM:  keyPEM,
		CAPEM:          caPEM,
		Certificate:    leaf,
		PrivateKey:     leafKey,
	}, nil
}

// VerifyLeaf cryptographically verifies a leaf against the CA: signature chain,
// hostname coverage, key usage, current validity (with skew tolerance), and
// that the leaf is not itself a CA.
func VerifyLeaf(leaf, caCert *x509.Certificate, expectDNS []string, now time.Time) error {
	if leaf == nil || caCert == nil {
		return errors.New("nil certificate")
	}
	roots := x509.NewCertPool()
	roots.AddCert(caCert)
	if _, err := leaf.Verify(x509.VerifyOptions{
		Roots:       roots,
		CurrentTime: now,
		KeyUsages:   []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}); err != nil {
		return fmt.Errorf("chain/signature/validity verification failed: %w", err)
	}
	got := normalizedSet(leaf.DNSNames)
	want := normalizedSet(expectDNS)
	if len(got) != len(want) {
		return fmt.Errorf("SAN mismatch: leaf has %v, want %v", sorted(got), sorted(want))
	}
	for n := range want {
		if !got[n] {
			return fmt.Errorf("SAN %q missing from leaf (have %v)", n, sorted(got))
		}
	}
	if leaf.IsCA {
		return errors.New("leaf must not be a CA")
	}
	return nil
}

// RenewalTime computes the instant renewal must start: notAfter - renewBefore,
// further pulled earlier by MaxSkew so clock skew cannot strand a cert.
func RenewalTime(notAfter, now time.Time, renewBefore time.Duration) time.Time {
	return notAfter.Add(-renewBefore).Add(-MaxSkew)
}

// NeedsRenewal reports whether the cert is at/after its renewal window,
// invalid for the current time, covers the wrong names, or does not chain.
func NeedsRenewal(leaf, caCert *x509.Certificate, wantDNS []string, renewBefore time.Duration, now time.Time) (bool, string) {
	if err := VerifyLeaf(leaf, caCert, wantDNS, now); err != nil {
		return true, "existing certificate fails verification: " + err.Error()
	}
	if !now.Before(RenewalTime(leaf.NotAfter, now, renewBefore)) {
		return true, fmt.Sprintf("within renewal window (renewalTime=%s notAfter=%s)",
			leaf.NotAfter.Add(-renewBefore).UTC().Format(time.RFC3339), leaf.NotAfter.UTC().Format(time.RFC3339))
	}
	return false, ""
}

// SerialString renders a serial as colon-separated uppercase hex, matching
// openssl x509 -serial style used in status/annotations.
func SerialString(s *big.Int) string {
	b := s.Bytes()
	if len(b) == 0 {
		b = []byte{0}
	}
	parts := make([]string, len(b))
	for i, c := range b {
		parts[i] = fmt.Sprintf("%02X", c)
	}
	return strings.Join(parts, ":")
}

// ThumbprintSHA256 returns the colon-hex SHA-256 fingerprint of DER cert.
func ThumbprintSHA256(der []byte) string {
	sum := sha256.Sum256(der)
	parts := make([]string, len(sum))
	for i, b := range sum {
		parts[i] = fmt.Sprintf("%02X", b)
	}
	return strings.Join(parts, ":")
}

// ParseCertificatePEM decodes the first CERTIFICATE block.
func ParseCertificatePEM(data []byte) (*x509.Certificate, error) {
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, errors.New("no PEM block found")
	}
	if block.Type != "CERTIFICATE" {
		return nil, fmt.Errorf("expected CERTIFICATE PEM, got %q", block.Type)
	}
	return x509.ParseCertificate(block.Bytes)
}

// ParsePrivateKeyPEM decodes PKCS#1 / PKCS#8 / EC private key PEM.
func ParsePrivateKeyPEM(data []byte) (crypto.Signer, error) {
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, errors.New("no PEM block found in key")
	}
	switch block.Type {
	case "PRIVATE KEY":
		k, err := x509.ParsePKCS8PrivateKey(block.Bytes)
		if err != nil {
			return nil, fmt.Errorf("parse PKCS#8 key: %w", err)
		}
		s, ok := k.(crypto.Signer)
		if !ok {
			return nil, errors.New("PKCS#8 key is not a signer")
		}
		return s, nil
	case "EC PRIVATE KEY":
		return x509.ParseECPrivateKey(block.Bytes)
	case "RSA PRIVATE KEY":
		return x509.ParsePKCS1PrivateKey(block.Bytes)
	default:
		return nil, fmt.Errorf("unsupported key PEM type %q", block.Type)
	}
}

// GenerateTestCA creates a fresh self-signed test CA. SAMPLE ONLY.
func GenerateTestCA(commonName string, now time.Time, lifetime time.Duration) (certPEM, keyPEM []byte, cert *x509.Certificate, signer crypto.Signer, err error) {
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	serial, err := randSerial()
	if err != nil {
		return nil, nil, nil, nil, err
	}
	tmpl := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkixName(commonName),
		NotBefore:             now.Add(-MaxSkew),
		NotAfter:              now.Add(lifetime),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature | x509.KeyUsageCRLSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &caKey.PublicKey, caKey)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	parsed, err := x509.ParseCertificate(der)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	certPEM = pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyDER, err := x509.MarshalPKCS8PrivateKey(caKey)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	keyPEM = pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER})
	return certPEM, keyPEM, parsed, caKey, nil
}

// CSRFromKey builds a real PKCS#10 CSR PEM for the key (used for pending Secret).
func CSRFromRequest(dnsNames []string, key *ecdsa.PrivateKey) ([]byte, error) {
	tmpl := x509.CertificateRequest{
		Subject:  pkixName(dnsNames[0]),
		DNSNames: append([]string(nil), dnsNames...),
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, &tmpl, key)
	if err != nil {
		return nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der}), nil
}

func newSerial(pinned *big.Int) *big.Int {
	if pinned != nil {
		return pinned
	}
	s, err := randSerial()
	if err != nil {
		panic(err) // crypto/rand failure is non-recoverable
	}
	return s
}

func randSerial() (*big.Int, error) {
	limit := new(big.Int).Lsh(big.NewInt(1), 128)
	s, err := rand.Int(rand.Reader, limit)
	if err != nil {
		return nil, err
	}
	return new(big.Int).SetBit(s, 127, 1), nil
}

func pkixName(cn string) pkix.Name {
	return pkix.Name{CommonName: cn}
}

func normalizedSet(in []string) map[string]bool {
	m := map[string]bool{}
	for _, s := range in {
		s = strings.TrimSpace(strings.ToLower(s))
		if s != "" {
			m[s] = true
		}
	}
	return m
}

func sorted(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func publicKeysEqual(a, b any) bool {
	ak, err := x509.MarshalPKIXPublicKey(a)
	if err != nil {
		return false
	}
	bk, err := x509.MarshalPKIXPublicKey(b)
	if err != nil {
		return false
	}
	return hex.EncodeToString(ak) == hex.EncodeToString(bk)
}
