package v1alpha1

import (
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"strings"
	"time"

	"github.com/example/cert-renewal-operator/internal/pki"
)

// EffectiveSpec is the normalized, defaulted view of CertificateSpec that
// issuance decisions are made against.
type EffectiveSpec struct {
	DNSNames    []string
	SecretName  string
	Duration    time.Duration
	RenewBefore time.Duration
	IssuerName  string
}

const (
	// DefaultDuration is used when spec.duration is omitted.
	DefaultDuration = 24 * time.Hour
	// MinDuration / MaxDuration bound requested validity. MinDuration must
	// exceed the clock skew tolerance so a defaulted renewBefore (duration/3)
	// still leaves a positive healthy window.
	MinDuration = pki.MaxSkew*2 + time.Minute
	MaxDuration = 24 * 365 * 10 * time.Hour
	// DefaultRenewBeforeFraction renews with 1/3 of lifetime remaining.
	DefaultRenewBeforeFraction = 3
)

// Normalize validates the spec and fills defaults. The DNS list is sorted and
// de-duplicated so order changes do not force pointless reissuance.
func (s *CertificateSpec) Normalize() (EffectiveSpec, error) {
	eff := EffectiveSpec{SecretName: s.SecretName, IssuerName: s.Issuer.Name}
	if strings.TrimSpace(eff.SecretName) == "" {
		return eff, errInvalid("secretName is required")
	}
	if strings.TrimSpace(eff.IssuerName) == "" {
		return eff, errInvalid("issuer.name is required")
	}
	seen := map[string]bool{}
	for _, n := range s.DNSNames {
		n = strings.TrimSpace(strings.ToLower(n))
		if n == "" {
			return eff, errInvalid("dnsNames entries must not be empty")
		}
		if !seen[n] {
			seen[n] = true
			eff.DNSNames = append(eff.DNSNames, n)
		}
	}
	if len(eff.DNSNames) == 0 {
		return eff, errInvalid("at least one dnsNames entry is required")
	}
	sort.Strings(eff.DNSNames)

	eff.Duration = DefaultDuration
	if s.Duration != nil {
		eff.Duration = s.Duration.Duration
	}
	if eff.Duration < MinDuration {
		return eff, errInvalid("duration must be >= " + MinDuration.String())
	}
	if eff.Duration > MaxDuration {
		return eff, errInvalid("duration must be <= " + MaxDuration.String())
	}

	if s.RenewBefore != nil {
		eff.RenewBefore = s.RenewBefore.Duration
		if eff.RenewBefore <= 0 {
			return eff, errInvalid("renewBefore must be positive")
		}
		// The renewal instant is notAfter - renewBefore - clockSkewTolerance.
		// To guarantee a strictly positive healthy period after issuance (and
		// therefore prevent an endless re-issue loop where every freshly issued
		// cert is already inside its window), we require:
		//     renewBefore + skewTolerance < duration.
		if max := eff.Duration - pki.MaxSkew; eff.RenewBefore >= max {
			return eff, errInvalid("renewBefore must be < duration - " + pki.MaxSkew.String() +
				" (" + max.String() + " here); otherwise a fresh certificate is immediately renewable")
		}
	} else {
		eff.RenewBefore = eff.Duration / DefaultRenewBeforeFraction
	}
	return eff, nil
}

type specErr string

func (e specErr) Error() string { return string(e) }
func errInvalid(m string) error { return specErr(m) }

// IsSpecInvalid reports whether the error is a static spec validation error.
func IsSpecInvalid(err error) bool {
	_, ok := err.(specErr)
	return ok
}

// Hash renders a stable SHA-256 over every spec field that affects the issued
// certificate or where it is stored. It is used to:
//   - bind the serving Secret to the spec generation that produced it, and
//   - reject stale issuance receipts after an in-flight spec change.
func (e EffectiveSpec) Hash() string {
	h := sha256.New()
	write := func(k, v string) {
		h.Write([]byte(k))
		h.Write([]byte{0})
		h.Write([]byte(v))
		h.Write([]byte{0})
	}
	write("secret", e.SecretName)
	write("issuer", e.IssuerName)
	write("duration", e.Duration.String())
	write("renewBefore", e.RenewBefore.String())
	write("dns", strings.Join(e.DNSNames, ","))
	return hex.EncodeToString(h.Sum(nil))[:32]
}
