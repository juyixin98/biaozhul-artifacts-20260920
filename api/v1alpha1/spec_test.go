package v1alpha1

import (
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/example/cert-renewal-operator/internal/pki"
)

func dur(d time.Duration) *metav1.Duration { return &metav1.Duration{Duration: d} }

func TestNormalizeRejectsBlankDNS(t *testing.T) {
	s := &CertificateSpec{DNSNames: []string{"  "}, SecretName: "s", Issuer: IssuerRef{Name: "ca"}}
	if _, err := s.Normalize(); err == nil {
		t.Fatal("expected error for blank dns name")
	}
}

func TestNormalizeValidDefaults(t *testing.T) {
	s := &CertificateSpec{DNSNames: []string{"x.local"}, SecretName: "s", Issuer: IssuerRef{Name: "ca"}}
	eff, err := s.Normalize()
	if err != nil {
		t.Fatal(err)
	}
	if eff.Duration != DefaultDuration {
		t.Fatalf("duration = %v want %v", eff.Duration, DefaultDuration)
	}
	if eff.RenewBefore != DefaultDuration/DefaultRenewBeforeFraction {
		t.Fatalf("renewBefore default wrong: %v", eff.RenewBefore)
	}
	// Sorted/deduped DNS.
	want := []string{"a.example.com", "b.example.com"}
	s2 := &CertificateSpec{DNSNames: []string{"b.example.com", "a.example.com", "a.example.com"}, SecretName: "s", Issuer: IssuerRef{Name: "ca"}}
	eff2, err := s2.Normalize()
	if err != nil {
		t.Fatal(err)
	}
	if len(eff2.DNSNames) != 2 || eff2.DNSNames[0] != want[0] || eff2.DNSNames[1] != want[1] {
		t.Fatalf("normalized DNS = %v want %v", eff2.DNSNames, want)
	}
	// Order-only change must NOT change the hash.
	s3 := &CertificateSpec{DNSNames: []string{"a.example.com", "b.example.com"}, SecretName: "s", Issuer: IssuerRef{Name: "ca"}}
	eff3, _ := s3.Normalize()
	if eff2.Hash() != eff3.Hash() {
		t.Fatal("spec hash must be invariant to DNS ordering/duplication")
	}
	if h := eff.Hash(); len(h) == 0 {
		t.Fatal("empty hash")
	}
}

func TestRenewBeforeInvariantPreventsHotLoop(t *testing.T) {
	// duration=20m; max renewBefore = duration - skew(5m) = 15m, strictly less.
	s := &CertificateSpec{
		DNSNames: []string{"x.local"}, SecretName: "s",
		Duration: dur(20 * time.Minute), RenewBefore: dur(15 * time.Minute),
		Issuer: IssuerRef{Name: "ca"},
	}
	if _, err := s.Normalize(); err == nil {
		t.Fatal("renewBefore == duration-skew must be rejected (fresh cert immediately renewable)")
	}
	s.RenewBefore = dur(14 * time.Minute)
	eff, err := s.Normalize()
	if err != nil {
		t.Fatalf("renewBefore just inside bound should be valid: %v", err)
	}
	// Healthy window must be strictly positive.
	if eff.Duration-eff.RenewBefore-pki.MaxSkew <= 0 {
		t.Fatal("healthy window must be > 0")
	}
}

func TestDurationBounds(t *testing.T) {
	tooShort := &CertificateSpec{DNSNames: []string{"x"}, SecretName: "s", Duration: dur(time.Minute), Issuer: IssuerRef{Name: "ca"}}
	if _, err := tooShort.Normalize(); err == nil {
		t.Fatal("duration below minimum must be rejected")
	}
	missing := &CertificateSpec{SecretName: "s", Issuer: IssuerRef{Name: "ca"}}
	if _, err := missing.Normalize(); err == nil {
		t.Fatal("missing dnsNames must be rejected")
	}
	noSecret := &CertificateSpec{DNSNames: []string{"x"}, Issuer: IssuerRef{Name: "ca"}}
	if _, err := noSecret.Normalize(); err == nil {
		t.Fatal("missing secretName must be rejected")
	}
	noIssuer := &CertificateSpec{DNSNames: []string{"x"}, SecretName: "s"}
	if _, err := noIssuer.Normalize(); err == nil {
		t.Fatal("missing issuer must be rejected")
	}
}
