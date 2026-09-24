// Package issuer implements the controller.Signer backed by the local test CA.
package issuer

import (
	"context"
	"crypto/ecdsa"
	"time"

	"github.com/example/cert-renewal-operator/internal/pki"
)

// LocalSigner performs real crypto/x509 signing through the loaded CA.
type LocalSigner struct{}

// Sign implements controller.Signer.
func (LocalSigner) Sign(_ context.Context, ca *pki.CA, dnsNames []string,
	duration time.Duration, pendingKey *ecdsa.PrivateKey, now time.Time) (*pki.IssueResult, error) {
	b, err := ca.IssueWithKey(pki.IssueRequest{
		DNSNames: dnsNames,
		Duration: duration,
		Now:      now,
	}, pendingKey)
	if err != nil {
		return nil, err
	}
	return &pki.IssueResult{Bundle: b}, nil
}
