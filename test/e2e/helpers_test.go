//go:build e2e

// Package e2e contains the kind end-to-end tests. They run against a real
// cluster with the controller deployed (see README "Acceptance"). Every
// cryptographic and protocol operation is real: CSRs are signed by the
// in-cluster test CA, certificate chains are verified with crypto/x509, and
// the issued leaf is served over a genuine TLS handshake with hostname
// verification.
package e2e_test

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"strings"
	"testing"
	"time"

	coordinatorv1alpha1 "github.com/example/certrenewal/api/v1alpha1"
	"github.com/example/certrenewal/internal/pki"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/rand"
	"k8s.io/apimachinery/pkg/util/wait"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	controllerNamespace = "cert-renewal-system"
	controllerDeploy    = "cert-renewal-coordinator"
	pollInterval        = 2 * time.Second
)

type env struct {
	t  *testing.T
	c  client.Client
	ns string
}

func newEnv(t *testing.T) *env {
	t.Helper()
	cfg, err := ctrl.GetConfig()
	if err != nil {
		t.Skipf("no cluster config available (run against a kind cluster): %v", err)
	}
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := coordinatorv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	c, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}

	// Fail fast with an actionable message when the CRDs are not installed.
	probe := &coordinatorv1alpha1.CertificateList{}
	if err := c.List(context.Background(), probe, client.Limit(1)); err != nil {
		t.Fatalf("Certificate CRD not served; run `make kind-load deploy sample-ca` first: %v", err)
	}

	ns := fmt.Sprintf("cert-e2e-%s", rand.String(6))
	if err := c.Create(context.Background(), &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = c.Delete(context.Background(), &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}})
	})
	return &env{t: t, c: c, ns: ns}
}

func (e *env) createCA(name string) {
	e.t.Helper()
	certPEM, keyPEM, err := pki.GenerateTestCA(time.Now(), 365*24*time.Hour)
	if err != nil {
		e.t.Fatal(err)
	}
	if err := e.c.Create(context.Background(), &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Namespace: e.ns, Name: name},
		Type:       corev1.SecretTypeTLS,
		Data: map[string][]byte{
			corev1.TLSCertKey:              certPEM,
			corev1.TLSPrivateKeyKey:        keyPEM,
			corev1.ServiceAccountRootCAKey: certPEM,
		},
	}); err != nil {
		e.t.Fatal(err)
	}
}

func (e *env) createCertificate(name, issuer string, dns []string, duration, renewBefore time.Duration) {
	e.t.Helper()
	spec := coordinatorv1alpha1.CertificateSpec{
		DNSNames:   dns,
		CommonName: dns[0],
		SecretName: name + "-tls",
		IssuerRef:  coordinatorv1alpha1.IssuerRef{Name: issuer},
	}
	if duration > 0 {
		spec.Duration = &metav1.Duration{Duration: duration}
	}
	if renewBefore > 0 {
		spec.RenewBefore = &metav1.Duration{Duration: renewBefore}
	}
	if err := e.c.Create(context.Background(), &coordinatorv1alpha1.Certificate{
		ObjectMeta: metav1.ObjectMeta{Namespace: e.ns, Name: name},
		Spec:       spec,
	}); err != nil {
		e.t.Fatal(err)
	}
}

func (e *env) getCert(name string) *coordinatorv1alpha1.Certificate {
	e.t.Helper()
	c := &coordinatorv1alpha1.Certificate{}
	if err := e.c.Get(context.Background(), types.NamespacedName{Namespace: e.ns, Name: name}, c); err != nil {
		e.t.Fatal(err)
	}
	return c
}

func (e *env) getSecret(name string) *corev1.Secret {
	e.t.Helper()
	s := &corev1.Secret{}
	if err := e.c.Get(context.Background(), types.NamespacedName{Namespace: e.ns, Name: name}, s); err != nil {
		e.t.Fatal(err)
	}
	return s
}

func (e *env) waitReady(name string, timeout time.Duration) *coordinatorv1alpha1.Certificate {
	e.t.Helper()
	var last string
	err := wait.PollUntilContextTimeout(context.Background(), pollInterval, timeout, true, func(ctx context.Context) (bool, error) {
		c := &coordinatorv1alpha1.Certificate{}
		if err := e.c.Get(ctx, types.NamespacedName{Namespace: e.ns, Name: name}, c); err != nil {
			return false, err
		}
		for _, cond := range c.Status.Conditions {
			if cond.Type == coordinatorv1alpha1.ConditionReady {
				last = string(cond.Status) + "/" + cond.Reason + ": " + cond.Message
				return cond.Status == metav1.ConditionTrue, nil
			}
		}
		last = "no Ready condition yet"
		return false, nil
	})
	if err != nil {
		e.t.Fatalf("certificate %s never became Ready (last: %s): %v", name, last, err)
	}
	return e.getCert(name)
}

func (e *env) waitRequestFailed(owner string, timeout time.Duration) {
	e.t.Helper()
	err := wait.PollUntilContextTimeout(context.Background(), pollInterval, timeout, true, func(ctx context.Context) (bool, error) {
		list := &coordinatorv1alpha1.CertificateRequestList{}
		if err := e.c.List(ctx, list, client.InNamespace(e.ns)); err != nil {
			return false, err
		}
		for _, cr := range list.Items {
			if cr.Annotations["coordinator.example.com/certificate-name"] != owner {
				continue
			}
			for _, cond := range cr.Status.Conditions {
				if cond.Type == coordinatorv1alpha1.ConditionReady && cond.Status == metav1.ConditionFalse {
					return true, nil
				}
			}
		}
		return false, nil
	})
	if err != nil {
		e.t.Fatalf("request for %s never reached a failed/retrying state: %v", owner, err)
	}
}

func (e *env) countRequests(owner string) int {
	list := &coordinatorv1alpha1.CertificateRequestList{}
	if err := e.c.List(context.Background(), list, client.InNamespace(e.ns)); err != nil {
		e.t.Fatal(err)
	}
	n := 0
	for _, cr := range list.Items {
		if cr.Annotations["coordinator.example.com/certificate-name"] == owner {
			n++
		}
	}
	return n
}

// verifyMaterial parses the Secret, runs real x509 chain+hostname validation
// for every requested name, asserts key/cert pairing and returns the leaf.
func verifyMaterial(t *testing.T, s *corev1.Secret, names []string, now time.Time) *x509.Certificate {
	t.Helper()
	leaf, err := pki.ParseFirstCertificate(s.Data["tls.crt"])
	if err != nil {
		t.Fatalf("parse tls.crt: %v", err)
	}
	caCerts, err := pki.ParseCertificates(s.Data["ca.crt"])
	if err != nil {
		t.Fatalf("parse ca.crt: %v", err)
	}
	signer, err := pki.ParsePrivateKey(s.Data["tls.key"])
	if err != nil {
		t.Fatalf("parse tls.key: %v", err)
	}
	if !pki.PublicKeyMatches(leaf, signer.Public()) {
		t.Fatal("tls.key does not match tls.crt")
	}
	pool := x509.NewCertPool()
	pool.AddCert(caCerts[0])
	for _, name := range names {
		if _, err := leaf.Verify(x509.VerifyOptions{
			Roots: pool, DNSName: name, CurrentTime: now,
			KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		}); err != nil {
			t.Fatalf("chain does not verify for %q: %v", name, err)
		}
	}
	return leaf
}

// httpsGETWithCert serves the issued leaf+key on a local TLS listener and
// dials it with hostname verification against caRoot. This exercises a real
// TLS handshake and real chain/domain validation — no mocks.
func httpsGETWithCert(t *testing.T, leafPEM, keyPEM, caPEM []byte, serverName string) {
	t.Helper()
	srvCert, err := tls.X509KeyPair(leafPEM, keyPEM)
	if err != nil {
		t.Fatal(err)
	}
	ln, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{Certificates: []tls.Certificate{srvCert}})
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				fmt.Fprintf(conn, "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
			}()
		}
	}()

	caCerts, err := pki.ParseCertificates(caPEM)
	if err != nil {
		t.Fatal(err)
	}
	roots := x509.NewCertPool()
	roots.AddCert(caCerts[0])
	conn, err := tls.Dial("tcp", ln.Addr().String(), &tls.Config{
		RootCAs:    roots,
		ServerName: serverName,
		MinVersion: tls.VersionTLS12,
	})
	if err != nil {
		t.Fatalf("TLS handshake for SNI %q failed: %v", serverName, err)
	}
	defer conn.Close()
	if len(conn.ConnectionState().VerifiedChains) == 0 {
		t.Fatal("TLS handshake reported no verified chains")
	}
	fmt.Fprintf(conn, "GET / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n", serverName)
	buf := make([]byte, 12)
	if _, err := conn.Read(buf); err != nil {
		t.Fatalf("reading HTTP response over TLS: %v", err)
	}
	if !strings.HasPrefix(string(buf), "HTTP/1.1 200") {
		t.Fatalf("unexpected status line: %q", strings.TrimSpace(string(buf)))
	}
}

func readyReason(c *coordinatorv1alpha1.Certificate) string {
	for _, cond := range c.Status.Conditions {
		if cond.Type == coordinatorv1alpha1.ConditionReady {
			return string(cond.Status) + "/" + cond.Reason
		}
	}
	return ""
}
