package pki

import (
	"crypto/ecdsa"
	"crypto/x509"
	"testing"
	"time"
)

func TestCAIssuanceAndChainVerification(t *testing.T) {
	now := time.Date(2026, 9, 23, 12, 0, 0, 0, time.UTC)
	caPEM, caKeyPEM, err := GenerateTestCA(now, 30*24*time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	caCerts, err := ParseCertificates(caPEM)
	if err != nil {
		t.Fatal(err)
	}
	if !caCerts[0].IsCA {
		t.Fatal("generated CA must have CA basic constraint")
	}
	caKey, err := ParsePrivateKey(caKeyPEM)
	if err != nil {
		t.Fatal(err)
	}

	csrPEM, leafKey, err := CreateCSR(CSRParams{CommonName: "svc.cluster.local", DNSNames: []string{"svc.cluster.local", "svc.default.svc"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ParsePrivateKey(func() []byte { b, _ := EncodePrivateKey(leafKey); return b }()); err != nil {
		t.Fatal(err)
	}

	res, err := SignCSR(caCerts[0], caKey, csrPEM, "svc.cluster.local",
		[]string{"svc.cluster.local", "svc.default.svc"}, now, now.Add(time.Hour))
	if err != nil {
		t.Fatalf("sign: %v", err)
	}

	leaf, err := ParseFirstCertificate(res.CertificatePEM)
	if err != nil {
		t.Fatal(err)
	}
	if err := VerifyChain(leaf, caCerts[0], "svc.default.svc", now.Add(30*time.Minute)); err != nil {
		t.Fatalf("chain verification failed: %v", err)
	}
	// Key usage is serverAuth only context; wrong-EKU check via explicit
	// client-only pool would be redundant; expiry boundary is real:
	if err := VerifyChain(leaf, caCerts[0], "svc.cluster.local", now.Add(2*time.Hour)); err == nil {
		t.Fatal("expired certificate must fail verification")
	}
	if _, err := leaf.Verify(x509.VerifyOptions{Roots: mustPool(caCerts[0]), DNSName: "other.example.com", CurrentTime: now}); err == nil {
		t.Fatal("certificate must not verify for an unlisted hostname")
	}
	if !leaf.PublicKey.(*ecdsa.PublicKey).Equal(&leafKey.PublicKey) {
		t.Fatal("signed public key is not the CSR public key")
	}
	if SerialHex(leaf) == "" {
		t.Fatal("empty serial")
	}
}

func TestSignCSRRejectsMismatchedDomains(t *testing.T) {
	now := time.Now().UTC()
	caPEM, caKeyPEM, err := GenerateTestCA(now, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	caCerts, _ := ParseCertificates(caPEM)
	caKey, _ := ParsePrivateKey(caKeyPEM)

	csrPEM, _, err := CreateCSR(CSRParams{CommonName: "a.example.com", DNSNames: []string{"a.example.com"}})
	if err != nil {
		t.Fatal(err)
	}
	// Attempt to use a valid CSR for a.example.com to obtain a certificate
	// claimed for b.example.com — must be refused.
	if _, err := SignCSR(caCerts[0], caKey, csrPEM, "a.example.com", []string{"b.example.com"}, now, now.Add(time.Hour)); err == nil {
		t.Fatal("SAN mismatch must be rejected")
	}
	if _, err := SignCSR(caCerts[0], caKey, csrPEM, "other-cn", []string{"a.example.com"}, now, now.Add(time.Hour)); err == nil {
		t.Fatal("CN mismatch must be rejected")
	}
}

func TestRenewalWindowBoundToValidity(t *testing.T) {
	nb := time.Unix(1_000_000, 0)
	na := nb.Add(24 * time.Hour)
	r := RenewalTime(nb, na, 8*time.Hour)
	if !r.Equal(nb.Add(16 * time.Hour)) {
		t.Fatalf("renewal = %s, want nb+16h", r)
	}
	// renewBefore >= lifetime clamps to notBefore.
	r2 := RenewalTime(nb, na, 24*time.Hour)
	if !r2.Equal(nb) {
		t.Fatalf("clamped renewal = %s, want nb", r2)
	}

	cases := []struct {
		name string
		now  time.Time
		want bool
	}{
		{"fresh", nb.Add(1 * time.Hour), false},
		{"just before window", r.Add(-time.Minute), false},
		{"just before window is not pulled earlier by skew", r.Add(-20 * time.Second), false},
		{"inside window", r.Add(time.Second), true},
		{"within skew of expiry", na.Add(-20 * time.Second), true},
		{"expired", na.Add(time.Minute), true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := NeedsRenewal(nb, na, r, tc.now, 30*time.Second); got != tc.want {
				t.Fatalf("NeedsRenewal=%v want %v", got, tc.want)
			}
		})
	}
}

func TestSpecFingerprint(t *testing.T) {
	h1 := SpecFingerprint("a.example.com", []string{"a.example.com"}, time.Hour)
	h2 := SpecFingerprint("a.example.com", []string{"a.example.com"}, time.Hour)
	if h1 != h2 {
		t.Fatal("identical specs must hash equal")
	}
	if h1 == SpecFingerprint("b.example.com", []string{"b.example.com"}, time.Hour) {
		t.Fatal("domain change must change hash")
	}
	if h1 == SpecFingerprint("a.example.com", []string{"a.example.com"}, 2*time.Hour) {
		t.Fatal("duration change must change hash")
	}
	// Order-insensitive for domains (set semantics).
	if SpecFingerprint("cn", []string{"a", "b"}, time.Hour) != SpecFingerprint("cn", []string{"b", "a"}, time.Hour) {
		t.Fatal("domain order must not affect hash")
	}
}

func TestSerialsAreUnique(t *testing.T) {
	now := time.Now().UTC()
	caPEM, caKeyPEM, err := GenerateTestCA(now, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	caCerts, _ := ParseCertificates(caPEM)
	caKey, _ := ParsePrivateKey(caKeyPEM)
	seen := map[string]bool{}
	for i := 0; i < 32; i++ {
		csrPEM, _, err := CreateCSR(CSRParams{CommonName: "h.example.com", DNSNames: []string{"h.example.com"}})
		if err != nil {
			t.Fatal(err)
		}
		res, err := SignCSR(caCerts[0], caKey, csrPEM, "h.example.com", []string{"h.example.com"}, now, now.Add(time.Hour))
		if err != nil {
			t.Fatal(err)
		}
		s := SerialHex(res.Certificate)
		if seen[s] {
			t.Fatalf("duplicate serial %s", s)
		}
		seen[s] = true
	}
}

func mustPool(ca *x509.Certificate) *x509.CertPool {
	p := x509.NewCertPool()
	p.AddCert(ca)
	return p
}
