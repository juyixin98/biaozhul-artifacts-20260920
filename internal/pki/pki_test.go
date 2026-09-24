package pki

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"encoding/pem"
	"testing"
	"time"
)

func fixedCA(t *testing.T, now time.Time, life time.Duration) (*CA, []byte, []byte) {
	t.Helper()
	certPEM, keyPEM, cert, signer, err := GenerateTestCA("Test CA", now, life)
	if err != nil {
		t.Fatalf("GenerateTestCA: %v", err)
	}
	return &CA{Certificate: cert, Signer: signer, RawCert: cert.Raw}, certPEM, keyPEM
}

func TestIssueAndVerifyRealChain(t *testing.T) {
	now := time.Now().UTC()
	ca, _, _ := fixedCA(t, now, 24*time.Hour)

	b, err := ca.Issue(IssueRequest{
		DNSNames: []string{"a.example.com", "b.example.com"},
		Duration: time.Hour,
		Now:      now,
	})
	if err != nil {
		t.Fatalf("Issue: %v", err)
	}

	// Re-parse from PEM exactly as a consumer/Secret reader would.
	leaf, err := ParseCertificatePEM(b.CertificatePEM)
	if err != nil {
		t.Fatalf("parse issued PEM: %v", err)
	}
	caFromPEM, err := ParseCertificatePEM(b.CAPEM)
	if err != nil {
		t.Fatalf("parse CA PEM: %v", err)
	}
	if err := VerifyLeaf(leaf, caFromPEM, []string{"a.example.com", "b.example.com"}, now); err != nil {
		t.Fatalf("VerifyLeaf: %v", err)
	}
	if got := leaf.SignatureAlgorithm; got != x509.ECDSAWithSHA256 {
		t.Fatalf("signature alg = %v, want ECDSA-SHA256", got)
	}
	if leaf.PublicKeyAlgorithm != x509.ECDSA {
		t.Fatalf("pubkey alg = %v, want ECDSA", leaf.PublicKeyAlgorithm)
	}
	// Key in the cert matches the returned private key.
	pub := leaf.PublicKey.(*ecdsa.PublicKey)
	if pub.X.Cmp(b.PrivateKey.PublicKey.X) != 0 || pub.Y.Cmp(b.PrivateKey.PublicKey.Y) != 0 {
		t.Fatal("certificate public key does not match returned private key")
	}
	// Round-trip the private key PEM.
	if _, err := ParsePrivateKeyPEM(b.PrivateKeyPEM); err != nil {
		t.Fatalf("parse issued key PEM: %v", err)
	}
	// Serial/thumbprint stability.
	if got := SerialString(leaf.SerialNumber); got == "" {
		t.Fatal("empty serial string")
	}
	if got := ThumbprintSHA256(leaf.Raw); len(got) != 95 { // 32 bytes => 64 hex + 31 colons
		t.Fatalf("thumbprint len = %d", len(got))
	}
}

func TestReusedPendingKeyKeepsPublicKey(t *testing.T) {
	now := time.Now().UTC()
	ca, _, _ := fixedCA(t, now, 24*time.Hour)

	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	b, err := ca.IssueWithKey(IssueRequest{
		DNSNames: []string{"reuse.example.com"},
		Duration: time.Hour,
		Now:      now,
	}, key)
	if err != nil {
		t.Fatalf("IssueWithKey: %v", err)
	}
	leaf := b.Certificate
	pub := leaf.PublicKey.(*ecdsa.PublicKey)
	if pub.X.Cmp(key.PublicKey.X) != 0 || pub.Y.Cmp(key.PublicKey.Y) != 0 {
		t.Fatal("issued cert did not reuse supplied pending key")
	}

	// A real CSR produced from that key validates and matches the key.
	csrPEM, err := CSRFromRequest([]string{"reuse.example.com"}, key)
	if err != nil {
		t.Fatal(err)
	}
	block, _ := pem.Decode(csrPEM)
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	if err := csr.CheckSignature(); err != nil {
		t.Fatalf("CSR self-signature invalid: %v", err)
	}
}

func TestVerifyRejectsWrongCAAndWrongSAN(t *testing.T) {
	now := time.Now().UTC()
	ca1, _, _ := fixedCA(t, now, 24*time.Hour)
	ca2, _, _ := fixedCA(t, now, 24*time.Hour)

	b, err := ca1.Issue(IssueRequest{DNSNames: []string{"good.example.com"}, Duration: time.Hour, Now: now})
	if err != nil {
		t.Fatal(err)
	}
	// Wrong CA must fail chain verification.
	if err := VerifyLeaf(b.Certificate, ca2.Certificate, []string{"good.example.com"}, now); err == nil {
		t.Fatal("expected verification failure against an unrelated CA")
	}
	// Missing SAN must fail.
	if err := VerifyLeaf(b.Certificate, ca1.Certificate, []string{"evil.example.com"}, now); err == nil {
		t.Fatal("expected SAN mismatch error")
	}
	// Extra requested SAN must fail.
	if err := VerifyLeaf(b.Certificate, ca1.Certificate, []string{"good.example.com", "extra.example.com"}, now); err == nil {
		t.Fatal("expected SAN mismatch for extra name")
	}
}

func TestRenewalWindowBoundToNotAfter(t *testing.T) {
	now := time.Now().UTC()
	ca, _, _ := fixedCA(t, now, 24*time.Hour)
	duration := 10 * time.Minute
	renewBefore := 4 * time.Minute

	b, err := ca.Issue(IssueRequest{DNSNames: []string{"w.example.com"}, Duration: duration, Now: now})
	if err != nil {
		t.Fatal(err)
	}

	// Immediately: far from expiry -> no renewal.
	if need, why := NeedsRenewal(b.Certificate, ca.Certificate, []string{"w.example.com"}, renewBefore, now); need {
		t.Fatalf("fresh cert should not need renewal: %s", why)
	}
	// One minute in: renewalTime = notAfter - 4m - 5m(skew) = now + 1m.
	atWindow := now.Add(61 * time.Second)
	need, _ := NeedsRenewal(b.Certificate, ca.Certificate, []string{"w.example.com"}, renewBefore, atWindow)
	if !need {
		t.Fatal("cert should need renewal once at/after renewalTime")
	}
	rt := RenewalTime(b.Certificate.NotAfter, now, renewBefore)
	if !rt.Equal(b.Certificate.NotAfter.Add(-renewBefore).Add(-MaxSkew)) {
		t.Fatalf("renewalTime not derived from notAfter: %v", rt)
	}
}

func TestClockSkewTolerance(t *testing.T) {
	now := time.Now().UTC()
	ca, _, _ := fixedCA(t, now, 24*time.Hour)
	b, err := ca.Issue(IssueRequest{DNSNames: []string{"skew.example.com"}, Duration: time.Hour, Now: now})
	if err != nil {
		t.Fatal(err)
	}
	// A controller clock 4m59s "behind" the real time still accepts the cert,
	// because NotBefore was backdated by MaxSkew (5m).
	behind := now.Add(-(MaxSkew - time.Second))
	if err := VerifyLeaf(b.Certificate, ca.Certificate, []string{"skew.example.com"}, behind); err != nil {
		t.Fatalf("cert should be valid with clock behind within MaxSkew: %v", err)
	}
	// More than MaxSkew behind => not yet valid.
	farBehind := now.Add(-(MaxSkew + time.Minute))
	if err := VerifyLeaf(b.Certificate, ca.Certificate, []string{"skew.example.com"}, farBehind); err == nil {
		t.Fatal("expected certificate to be not-yet-valid beyond skew tolerance")
	}
}

func TestParseCARejectsNonCAAndMismatchedKey(t *testing.T) {
	now := time.Now().UTC()
	ca, certPEM, _ := fixedCA(t, now, 24*time.Hour)
	// A leaf is not a CA.
	b, err := ca.Issue(IssueRequest{DNSNames: []string{"leaf"}, Duration: time.Hour, Now: now})
	if err != nil {
		t.Fatal(err)
	}
	_, keyPEM, _, key, err := GenerateTestCA("other", now, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	_ = certPEM
	// leaf cert + any key rejected: not a CA.
	if _, err := ParseCA(b.CertificatePEM, keyPEM); err == nil {
		t.Fatal("expected ParseCA to reject a leaf certificate")
	}
	// CA cert + a different key rejected.
	otherKey, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	otherPEM := pemEncodePKCS8(otherKey)
	if _, err := ParseCA(certPEM, otherPEM); err == nil {
		t.Fatal("expected ParseCA to reject mismatched cert/key")
	}
}

func TestIssueRejectsValidityBeyondCA(t *testing.T) {
	now := time.Now().UTC()
	ca, _, _ := fixedCA(t, now, time.Hour)
	if _, err := ca.Issue(IssueRequest{DNSNames: []string{"x"}, Duration: 2 * time.Hour, Now: now}); err == nil {
		t.Fatal("expected issuance beyond CA NotAfter to fail")
	}
}

func pemEncodePKCS8(der []byte) []byte {
	return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})
}
