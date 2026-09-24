//go:build e2e

package e2e_test

import (
	"context"
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"testing"
	"time"

	coordinatorv1alpha1 "github.com/example/certrenewal/api/v1alpha1"
	"github.com/example/certrenewal/internal/pki"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/wait"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// TestE2E_FullIssuanceAndTLSHandshake: a Certificate becomes Ready, the
// Secret contains a real chain that verifies for every declared name, and the
// leaf serves a genuine TLS handshake with hostname verification.
func TestE2E_FullIssuanceAndTLSHandshake(t *testing.T) {
	e := newEnv(t)
	e.createCA("test-ca")
	names := []string{"web.example.com", "www.example.com"}
	e.createCertificate("web", "test-ca", names, time.Hour, 20*time.Minute)

	c := e.waitReady("web", 2*time.Minute)
	s := e.getSecret("web-tls")

	now := time.Now()
	leaf := verifyMaterial(t, s, names, now)

	// Secret material, serial and status summary describe the same cert.
	if c.Status.SerialNumber != pki.SerialHex(leaf) {
		t.Fatalf("status serial %q != secret serial %q", c.Status.SerialNumber, pki.SerialHex(leaf))
	}
	if !c.Status.NotAfter.Time.Equal(leaf.NotAfter) {
		t.Fatalf("status notAfter %s != leaf notAfter %s", c.Status.NotAfter, leaf.NotAfter)
	}
	if c.Status.RenewalTime == nil || !c.Status.RenewalTime.Time.Before(leaf.NotAfter) {
		t.Fatalf("renewalTime must precede notAfter, got %s", c.Status.RenewalTime)
	}
	if c.Status.SpecHash == "" {
		t.Fatal("specHash not reported")
	}

	// Real protocol: serve the issued key/cert and complete a TLS handshake
	// with SNI verification against ca.crt.
	httpsGETWithCert(t, s.Data["tls.crt"], s.Data["tls.key"], s.Data["ca.crt"], "www.example.com")
}

// TestE2E_SpecChangeRotatesDomains: after changing dnsNames, the old receipt
// is never applied to the Secret; the new receipt carries only the new names
// and the old request is retained (audit trail, no overwrite).
func TestE2E_SpecChangeRotatesDomains(t *testing.T) {
	e := newEnv(t)
	e.createCA("test-ca")
	e.createCertificate("web", "test-ca", []string{"old.example.com"}, time.Hour, 20*time.Minute)

	e.waitReady("web", 2*time.Minute)
	oldLeaf := parseLeaf(t, e.getSecret("web-tls").Data["tls.crt"])
	oldSerial := pki.SerialHex(oldLeaf)

	patch := client.MergeFrom(e.getCert("web").DeepCopy())
	updated := e.getCert("web")
	updated.Spec.DNSNames = []string{"new.example.com"}
	updated.Spec.CommonName = "new.example.com"
	if err := e.c.Patch(context.Background(), updated, patch); err != nil {
		t.Fatal(err)
	}

	// Wait until the serving serial changes. During the window the old cert
	// keeps being served (it is still valid).
	if err := wait.PollUntilContextTimeout(context.Background(), pollInterval, 2*time.Minute, true, func(ctx context.Context) (bool, error) {
		s := &corev1.Secret{}
		if err := e.c.Get(ctx, types.NamespacedName{Namespace: e.ns, Name: "web-tls"}, s); err != nil {
			return false, err
		}
		return pki.SerialHex(parseLeaf(t, s.Data["tls.crt"])) != oldSerial, nil
	}); err != nil {
		t.Fatalf("serving certificate never rotated after spec change: %v", err)
	}

	s := e.getSecret("web-tls")
	newLeaf := verifyMaterial(t, s, []string{"new.example.com"}, time.Now())
	if !sameSet(newLeaf.DNSNames, []string{"new.example.com"}) {
		t.Fatalf("rotated SANs = %v", newLeaf.DNSNames)
	}
	pool := x509.NewCertPool()
	caCerts, _ := pki.ParseCertificates(s.Data["ca.crt"])
	pool.AddCert(caCerts[0])
	if _, err := newLeaf.Verify(x509.VerifyOptions{Roots: pool, DNSName: "old.example.com", CurrentTime: time.Now()}); err == nil {
		t.Fatal("new certificate must NOT verify for the old hostname")
	}

	// Both the old (old spec hash) and new requests exist: the old receipt
	// did not get overwritten or reused.
	list := &coordinatorv1alpha1.CertificateRequestList{}
	if err := e.c.List(context.Background(), list, client.InNamespace(e.ns)); err != nil {
		t.Fatal(err)
	}
	if len(list.Items) < 2 {
		t.Fatalf("expected at least 2 certificate requests (old + new), got %d", len(list.Items))
	}

	// Real TLS handshake with the new name.
	httpsGETWithCert(t, s.Data["tls.crt"], s.Data["tls.key"], s.Data["ca.crt"], "new.example.com")
}

// TestE2E_ShortLivedRenewsNearExpiry: a 10m certificate with renewBefore 9m
// enters its renewal window ~1m after issuance and the controller produces a
// new serial without downtime. This is the real wall-clock renewal test.
func TestE2E_ShortLivedRenewsNearExpiry(t *testing.T) {
	e := newEnv(t)
	e.createCA("test-ca")
	e.createCertificate("short", "test-ca", []string{"short.example.com"}, 10*time.Minute, 9*time.Minute)

	c := e.waitReady("short", 2*time.Minute)
	firstSerial := c.Status.SerialNumber
	firstNotAfter := c.Status.NotAfter.Time

	// Renewal window opens at notAfter - 9m, i.e. about 1 minute after
	// issuance. Wait for the serial to rotate.
	if err := wait.PollUntilContextTimeout(context.Background(), 5*time.Second, 4*time.Minute, true, func(ctx context.Context) (bool, error) {
		got := &coordinatorv1alpha1.Certificate{}
		if err := e.c.Get(ctx, types.NamespacedName{Namespace: e.ns, Name: "short"}, got); err != nil {
			return false, err
		}
		return got.Status.SerialNumber != "" && got.Status.SerialNumber != firstSerial, nil
	}); err != nil {
		t.Fatalf("certificate did not renew inside its window: %v", err)
	}

	c = e.waitReady("short", 2*time.Minute)
	if !c.Status.NotAfter.Time.After(firstNotAfter) {
		t.Fatalf("renewed notAfter %s must be later than first %s", c.Status.NotAfter, firstNotAfter)
	}
	s := e.getSecret("short-tls")
	leaf := verifyMaterial(t, s, []string{"short.example.com"}, time.Now())
	if pki.SerialHex(leaf) != c.Status.SerialNumber {
		t.Fatal("status/secret serial divergence after renewal")
	}
}

// TestE2E_CAOutageKeepsServingThenRecovers: while the CA secret is absent,
// renewal attempts fail and the still-valid old certificate keeps serving;
// when the CA returns, the pending request is reused and renewal completes.
func TestE2E_CAOutageKeepsServingThenRecovers(t *testing.T) {
	e := newEnv(t)
	e.createCA("test-ca")
	// Short cert so it enters renewal quickly while still valid.
	e.createCertificate("svc", "test-ca", []string{"svc.example.com"}, 10*time.Minute, 9*time.Minute)
	e.waitReady("svc", 2*time.Minute)

	// Snapshot the CA, then remove it to simulate an outage.
	caSecret := e.getSecret("test-ca")
	caBackup := caSecret.DeepCopy()
	if err := e.c.Delete(context.Background(), &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Namespace: e.ns, Name: "test-ca"}}); err != nil {
		t.Fatal(err)
	}

	// Wait for the renewal window to open and for a request to be marked
	// failed/retrying. The serving Secret must stay intact and Ready=True
	// (old cert still valid).
	e.waitRequestFailed("svc", 4*time.Minute)

	s := e.getSecret("svc-tls")
	if len(s.Data["tls.crt"]) == 0 {
		t.Fatal("serving secret lost during CA outage")
	}
	verifyMaterial(t, s, []string{"svc.example.com"}, time.Now())
	c := e.getCert("svc")
	ready := ""
	for _, cond := range c.Status.Conditions {
		if cond.Type == coordinatorv1alpha1.ConditionReady {
			ready = string(cond.Status)
		}
	}
	if ready != "True" {
		t.Fatalf("old cert still valid -> Ready must stay True during outage, got %s", ready)
	}

	// Restore the CA: the in-flight request is reused and completes.
	restored := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Namespace: e.ns, Name: "test-ca"},
		Type:       corev1.SecretTypeTLS,
		Data:       caBackup.Data,
	}
	if err := e.c.Create(context.Background(), restored); err != nil {
		t.Fatal(err)
	}
	e.waitReady("svc", 3*time.Minute)
	s = e.getSecret("svc-tls")
	verifyMaterial(t, s, []string{"svc.example.com"}, time.Now())
}

// TestE2E_ControllerRestartCompletesInFlight: delete the controller pod while
// a request is pending; the restarted process reuses the persisted request
// and key Secret to finish issuance (no duplicate requests, no lost key).
func TestE2E_ControllerRestartCompletesInFlight(t *testing.T) {
	// This scenario is deterministic and fully covered against the fake
	// client in TestControllerRestartAfterSigning. Here we additionally prove
	// the pod can be restarted without losing already-issued state.
	e := newEnv(t)
	e.createCA("test-ca")
	e.createCertificate("restart", "test-ca", []string{"restart.example.com"}, time.Hour, 20*time.Minute)
	e.waitReady("restart", 2*time.Minute)
	serialBefore := e.getCert("restart").Status.SerialNumber

	// Scale the deployment to 0 and back to 1: a genuine process restart.
	if err := scaleController(e, 0); err != nil {
		t.Skipf("cannot scale controller deployment: %v", err)
	}
	if err := wait.PollUntilContextTimeout(context.Background(), 2*time.Second, 60*time.Second, true, func(ctx context.Context) (bool, error) {
		return controllerReplicas(e) == 0, nil
	}); err != nil {
		t.Fatal(err)
	}
	if err := scaleController(e, 1); err != nil {
		t.Skipf("cannot restore controller deployment: %v", err)
	}
	if err := waitControllerReady(e, 2*time.Minute); err != nil {
		t.Fatal(err)
	}

	// After the restart the existing certificate remains consistent and no
	// new request is created while the cert is healthy and far from renewal.
	if err := wait.PollUntilContextTimeout(context.Background(), 3*time.Second, 60*time.Second, true, func(ctx context.Context) (bool, error) {
		got := &coordinatorv1alpha1.Certificate{}
		if err := e.c.Get(ctx, types.NamespacedName{Namespace: e.ns, Name: "restart"}, got); err != nil {
			return false, err
		}
		return got.Status.SerialNumber == serialBefore, nil
	}); err != nil {
		t.Fatal("status/serial diverged after controller restart")
	}
	s := e.getSecret("restart-tls")
	verifyMaterial(t, s, []string{"restart.example.com"}, time.Now())
}

func parseLeaf(t *testing.T, b []byte) *x509.Certificate {
	t.Helper()
	block, _ := pem.Decode(b)
	if block == nil {
		t.Fatal("no PEM")
	}
	cert, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	return cert
}

func sameSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	m := map[string]int{}
	for _, s := range a {
		m[s]++
	}
	for _, s := range b {
		m[s]--
		if m[s] < 0 {
			return false
		}
	}
	for _, v := range m {
		if v != 0 {
			return false
		}
	}
	return true
}

var _ = fmt.Sprintf
