package ca

import (
	"context"
	"crypto"
	"crypto/ecdsa"
	"crypto/rsa"
	"crypto/x509"
	"errors"
	"fmt"

	"github.com/example/certrenewal/internal/pki"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// ErrCAAbsent is returned when the referenced CA secret does not exist.
var ErrCAAbsent = errors.New("CA secret absent")

// CA is a loaded signer: the CA certificate and its private key.
type CA struct {
	Certificate *x509.Certificate
	Key         crypto.Signer
}

// Source loads CA material on demand so CA rotation is picked up without a
// restart and so a missing/broken CA does not crash the manager.
type Source interface {
	Get(ctx context.Context, namespace, name string) (*CA, error)
}

// SecretSource reads kubernetes.io/tls Secrets. tls.key is the signing key;
// the certificate is tls.crt (or ca.crt when present).
type SecretSource struct {
	Client client.Client
}

// NewSecretSource builds a Source backed by the API server.
func NewSecretSource(c client.Client) *SecretSource { return &SecretSource{Client: c} }

// Get loads and validates the CA secret.
func (s *SecretSource) Get(ctx context.Context, namespace, name string) (*CA, error) {
	secret := &corev1.Secret{}
	err := s.Client.Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, secret)
	if apierrors.IsNotFound(err) {
		return nil, fmt.Errorf("%w: %s/%s", ErrCAAbsent, namespace, name)
	}
	if err != nil {
		return nil, fmt.Errorf("get CA secret: %w", err)
	}

	keyBytes := secret.Data[corev1.TLSPrivateKeyKey]
	if len(keyBytes) == 0 {
		return nil, errors.New("CA secret is missing tls.key")
	}
	key, err := pki.ParsePrivateKey(keyBytes)
	if err != nil {
		return nil, fmt.Errorf("CA tls.key: %w", err)
	}

	certBytes := secret.Data[corev1.TLSCertKey]
	if len(certBytes) == 0 {
		certBytes = secret.Data[corev1.ServiceAccountRootCAKey] // ca.crt
	}
	if len(certBytes) == 0 {
		return nil, errors.New("CA secret is missing tls.crt/ca.crt")
	}
	certs, err := pki.ParseCertificates(certBytes)
	if err != nil {
		return nil, fmt.Errorf("CA tls.crt: %w", err)
	}
	caCert := certs[0]
	if !caCert.IsCA {
		return nil, errors.New("referenced CA certificate is not a CA (basicConstraints CA:TRUE missing)")
	}
	if !caCert.BasicConstraintsValid {
		return nil, errors.New("referenced CA certificate has invalid basic constraints")
	}
	if err := verifyKeyMatchesCert(caCert, key); err != nil {
		return nil, err
	}
	return &CA{Certificate: caCert, Key: key}, nil
}

func verifyKeyMatchesCert(cert *x509.Certificate, key crypto.Signer) error {
	switch pub := cert.PublicKey.(type) {
	case *ecdsa.PublicKey:
		ourPub, ok := key.Public().(*ecdsa.PublicKey)
		if !ok || !pub.Equal(ourPub) {
			return errors.New("CA private key does not match certificate")
		}
	case *rsa.PublicKey:
		ourPub, ok := key.Public().(*rsa.PublicKey)
		if !ok || !pub.Equal(ourPub) {
			return errors.New("CA private key does not match certificate")
		}
	default:
		return errors.New("unsupported CA public key algorithm")
	}
	return nil
}
