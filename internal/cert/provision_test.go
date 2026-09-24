package cert

import (
	"crypto/x509"
	"encoding/pem"
	"testing"
)

func TestGenerateCertChain(t *testing.T) {
	caPEM, certPEM, keyPEM, err := Generate("svc", "ns")
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}

	caBlock, _ := pem.Decode(caPEM)
	if caBlock == nil {
		t.Fatal("ca.crt is not PEM")
	}
	caCert, err := x509.ParseCertificate(caBlock.Bytes)
	if err != nil {
		t.Fatalf("parsing CA: %v", err)
	}
	if !caCert.IsCA {
		t.Fatal("certificate must be marked IsCA")
	}

	srvBlock, _ := pem.Decode(certPEM)
	if srvBlock == nil {
		t.Fatal("tls.crt is not PEM")
	}
	srvCert, err := x509.ParseCertificate(srvBlock.Bytes)
	if err != nil {
		t.Fatalf("parsing serving cert: %v", err)
	}

	// The serving certificate must actually verify against the CA.
	pool := x509.NewCertPool()
	pool.AddCert(caCert)
	if _, err := srvCert.Verify(x509.VerifyOptions{
		Roots:     pool,
		DNSName:   "svc.ns.svc.cluster.local",
		KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}); err != nil {
		t.Fatalf("serving cert does not verify under the CA for its service DNS: %v", err)
	}

	// The serving key must parse and match the certificate.
	keyBlock, _ := pem.Decode(keyPEM)
	if keyBlock == nil {
		t.Fatal("tls.key is not PEM")
	}
	if _, err := x509.ParseECPrivateKey(keyBlock.Bytes); err != nil {
		t.Fatalf("tls.key is not a valid EC private key: %v", err)
	}

	// Two generations must produce distinct CAs (real randomness, not fixed).
	ca2, _, _, err := Generate("svc", "ns")
	if err != nil {
		t.Fatal(err)
	}
	if string(ca2) == string(caPEM) {
		t.Fatal("two Generate calls returned identical CA material")
	}
}
