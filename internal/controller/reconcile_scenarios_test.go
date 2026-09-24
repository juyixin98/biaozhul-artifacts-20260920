package controller

import (
	"crypto/ecdsa"
	"sync/atomic"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	certv1alpha1 "github.com/example/cert-renewal-operator/api/v1alpha1"
	"github.com/example/cert-renewal-operator/internal/pki"
)

var epoch = time.Date(2026, 9, 23, 12, 0, 0, 0, time.UTC)

// 1. Initial issuance produces a real, consistent chain; Secret/status/serial agree.
func TestInitialIssuanceRealChainAndConsistency(t *testing.T) {
	h := newHarness(t, epoch)
	h.createCert("app", baseSpec("app-tls", "app.local", "www.app.local"))

	if res, err := h.reconcile("app"); err != nil {
		t.Fatalf("reconcile: %v", err)
	} else if res.RequeueAfter <= 0 {
		t.Fatalf("expected positive requeue, got %v", res.RequeueAfter)
	}

	leaf := h.assertConsistent("app", []string{"app.local", "www.app.local"})
	if _, ok := h.secretMaybe("app-tls-pending"); ok {
		t.Fatal("pending secret must be removed after successful issuance")
	}
	if !leaf.NotBefore.Before(epoch) || !leaf.NotBefore.After(epoch.Add(-pki.MaxSkew-time.Second)) {
		t.Fatalf("NotBefore not backdated within skew: %v", leaf.NotBefore)
	}
	if !leaf.NotAfter.Equal(epoch.Add(certv1alpha1.DefaultDuration)) {
		t.Fatalf("NotAfter = %v want %v", leaf.NotAfter, epoch.Add(certv1alpha1.DefaultDuration))
	}
}

// 2. Near-expiry: once the real cert enters its renewal window it is replaced
// with a fresh chain, serial rotates, and the new chain verifies.
func TestNearExpiryRenewsBoundToCertificateWindow(t *testing.T) {
	h := newHarness(t, epoch)
	spec := durSpec("ne-tls", 15*time.Minute, 9*time.Minute, "ne.local")
	h.createCert("ne", spec)
	if _, err := h.reconcile("ne"); err != nil {
		t.Fatal(err)
	}
	first := h.assertConsistent("ne", []string{"ne.local"})
	firstSerial := first.SerialNumber
	firstNotAfter := first.NotAfter

	// 30s in: still outside window -> no renewal, same serial.
	h.clock.advance(30 * time.Second)
	if _, err := h.reconcile("ne"); err != nil {
		t.Fatal(err)
	}
	if got := leafOf(t, h.getSecret("ne-tls")).SerialNumber; got.Cmp(firstSerial) != 0 {
		t.Fatal("certificate renewed before the window opened")
	}

	// renewalTime = notAfter - 9m - 5m(skew) = issuance + 1m. Advance 61s.
	h.clock.advance(31 * time.Second) // total 61s
	if _, err := h.reconcile("ne"); err != nil {
		t.Fatal(err)
	}
	second := h.assertConsistent("ne", []string{"ne.local"})
	if second.SerialNumber.Cmp(firstSerial) == 0 {
		t.Fatal("serial did not rotate after renewal")
	}
	if !second.NotAfter.Equal(firstNotAfter.Add(61 * time.Second)) {
		t.Fatalf("new NotAfter %v not based on renewed issuance time %v", second.NotAfter, firstNotAfter.Add(61*time.Second))
	}
	// renewed certificate's SAN really covers the domain at the new time
	if err := pki.VerifyLeaf(second, h.ca.Certificate, []string{"ne.local"}, h.clock.Now()); err != nil {
		t.Fatalf("renewed chain invalid: %v", err)
	}
}

// 3. Clock skew: a controller clock within +/- skew tolerates the cert; beyond
// tolerance an expired/abnormal cert is renewed, and an issuance beyond CA
// lifetime is rejected without deleting the serving secret.
func TestClockSkew(t *testing.T) {
	h := newHarness(t, epoch)
	spec := durSpec("sk-tls", time.Hour, 30*time.Minute, "sk.local")
	h.createCert("sk", spec)
	if _, err := h.reconcile("sk"); err != nil {
		t.Fatal(err)
	}
	first := h.assertConsistent("sk", []string{"sk.local"})

	// Controller clock 4m59s behind issuance instant (near the NotBefore edge).
	h.clock.set(epoch.Add(-(pki.MaxSkew - time.Second)))
	if _, err := h.reconcile("sk"); err != nil {
		t.Fatal(err)
	}
	if got := leafOf(t, h.getSecret("sk-tls")).SerialNumber; got.Cmp(first.SerialNumber) != 0 {
		t.Fatal("cert renewed despite being within skew tolerance")
	}

	// Controller clock 6m behind: cert not yet valid at that time -> renew attempt;
	// the freshly issued cert IS valid at the earlier time due to backdating.
	h.clock.set(epoch.Add(-(pki.MaxSkew + time.Minute)))
	if _, err := h.reconcile("sk"); err != nil {
		t.Fatal(err)
	}
	second := h.assertConsistent("sk", []string{"sk.local"})
	if second.SerialNumber.Cmp(first.SerialNumber) == 0 {
		t.Fatal("expected renewal when existing cert fails verification at skewed time")
	}
}

// 4. Secret update conflict: a concurrent writer bumps the Secret RV while the
// controller is committing; the controller retries and the new chain lands.
func TestSecretUpdateConflictRetried(t *testing.T) {
	h := newHarness(t, epoch)
	spec := durSpec("cf-tls", 15*time.Minute, 9*time.Minute, "cf.local")
	h.createCert("cf", spec)
	if _, err := h.reconcile("cf"); err != nil {
		t.Fatal(err)
	}
	first := h.assertConsistent("cf", []string{"cf.local"})

	// Advance into the renewal window (renewalTime = issuance + 1m) before the
	// conflicting renewal, so a new issuance is genuinely required.
	h.clock.advance(61 * time.Second)
	var fired int32
	h.reconcil.BeforeCommitSecret = func() {
		if atomic.AddInt32(&fired, 1) == 1 {
			// Concurrent no-op writer bumps RV once.
			sec := &corev1.Secret{}
			if err := h.c.Get(h.ctx, types.NamespacedName{Namespace: h.ns, Name: "cf-tls"}, sec); err != nil {
				t.Errorf("concurrent get: %v", err)
				return
			}
			if sec.Annotations == nil {
				sec.Annotations = map[string]string{}
			}
			sec.Annotations["concurrent.example.com/touch"] = "1"
			if err := h.c.Update(h.ctx, sec); err != nil {
				t.Errorf("concurrent update: %v", err)
			}
		}
	}
	if _, err := h.reconcile("cf"); err != nil {
		t.Fatalf("reconcile after conflict: %v", err)
	}
	second := h.assertConsistent("cf", []string{"cf.local"})
	if second.SerialNumber.Cmp(first.SerialNumber) == 0 {
		t.Fatal("expected new cert after conflict-retry commit")
	}
	if atomic.LoadInt32(&fired) < 2 {
		t.Fatalf("expected at least one commit attempt + one retry, got %d", fired)
	}
	sec := h.getSecret("cf-tls")
	if sec.Annotations["concurrent.example.com/touch"] != "1" {
		t.Fatal("concurrent annotation should survive the controller's read-modify-write retry")
	}
}

// 5. Spec change (domains) reissues; status/specHash track the new config and
// the new chain really carries the new SAN set.
func TestSpecChangeReissuesWithNewDomains(t *testing.T) {
	h := newHarness(t, epoch)
	h.createCert("ch", baseSpec("ch-tls", "old.local"))
	if _, err := h.reconcile("ch"); err != nil {
		t.Fatal(err)
	}
	old := h.assertConsistent("ch", []string{"old.local"})

	cr := h.getCert("ch")
	cr.Spec.DNSNames = []string{"new.local", "old.local"}
	// emulate a real apiserver generation bump
	cr.Generation = 1
	if cr.Generation == 0 {
		cr.Generation = 1
	}
	if err := h.c.Update(h.ctx, cr); err != nil {
		t.Fatal(err)
	}

	if _, err := h.reconcile("ch"); err != nil {
		t.Fatal(err)
	}
	newLeaf := h.assertConsistent("ch", []string{"new.local", "old.local"})
	if newLeaf.SerialNumber.Cmp(old.SerialNumber) == 0 {
		t.Fatal("serial should rotate on domain change")
	}
	// old domain-only cert must not validate against the new requirement
	if err := pki.VerifyLeaf(old, h.ca.Certificate, []string{"new.local", "old.local"}, epoch); err == nil {
		t.Fatal("old certificate unexpectedly covered new.local")
	}
}

// 6. Old-generation issuance receipt must NOT overwrite the newer DNS config:
// spec changes while signing is "in flight" -> receipt is discarded and a new
// issuance for the new domains follows.
func TestStaleGenerationReceiptDiscarded(t *testing.T) {
	h := newHarness(t, epoch)
	h.createCert("st", baseSpec("st-tls", "v1.local"))
	if _, err := h.reconcile("st"); err != nil {
		t.Fatal(err)
	}
	v1 := h.assertConsistent("st", []string{"v1.local"})

	// Trigger a renewal (spec change to v2 domains) but, during the Sign call,
	// mutate the spec to v3 to simulate a newer configuration arriving after the
	// signing for v2 started.
	cr := h.getCert("st")
	cr.Spec.DNSNames = []string{"v2.local"}
	cr.Generation = 2
	if err := h.c.Update(h.ctx, cr); err != nil {
		t.Fatal(err)
	}
	var entered int32
	h.signer.onSign = func(attempt int, dns []string, key *ecdsa.PrivateKey) {
		if atomic.AddInt32(&entered, 1) == 1 {
			// While the v2 receipt is being produced, change spec to v3.
			live := &certv1alpha1.Certificate{}
			if err := h.c.Get(h.ctx, h.nn("st"), live); err != nil {
				t.Errorf("get live: %v", err)
				return
			}
			live.Spec.DNSNames = []string{"v3.local"}
			live.Generation = 3
			if err := h.c.Update(h.ctx, live); err != nil {
				t.Errorf("update to v3: %v", err)
			}
		}
	}
	res, err := h.reconcile("st")
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if !res.Requeue && res.RequeueAfter <= 0 {
		t.Fatalf("expected requeue after discarding stale receipt, got %+v", res)
	}
	// Serving secret must still be the v1 certificate (stale v2 receipt dropped).
	after := leafOf(t, h.getSecret("st-tls"))
	if after.SerialNumber.Cmp(v1.SerialNumber) != 0 {
		t.Fatal("stale v2 receipt overwrote the serving secret before v3 issuance")
	}
	// Next reconcile issues for v3.
	h.signer.onSign = nil
	if _, err := h.reconcile("st"); err != nil {
		t.Fatal(err)
	}
	h.assertConsistent("st", []string{"v3.local"})
}

// 7. Issuance failure with NO serving secret: nothing is created, Ready=False,
// then a later success issues normally.
func TestIssuanceFailureNoSecretThenRecovery(t *testing.T) {
	h := newHarness(t, epoch)
	h.createCert("fn", baseSpec("fn-tls", "fn.local"))
	h.signer.failRemaining = 1

	if _, err := h.reconcile("fn"); err != nil {
		t.Fatalf("reconcile must not return hard error on signing failure: %v", err)
	}
	if _, ok := h.secretMaybe("fn-tls"); ok {
		t.Fatal("serving secret must not be created on failed issuance")
	}
	cr := h.getCert("fn")
	if c := condOf(cr, string(certv1alpha1.ConditionReady)); c == nil || c.Status != metav1.ConditionFalse {
		t.Fatalf("Ready should be False: %+v", c)
	}
	if c := condOf(cr, string(certv1alpha1.ConditionReady)); c == nil || c.Reason != certv1alpha1.ReasonIssuanceFailed {
		t.Fatalf("reason = %v want IssuanceFailed", c)
	}
	// pending application persists across the failure for reuse.
	if _, ok := h.secretMaybe("fn-tls-pending"); !ok {
		t.Fatal("pending secret should persist after failed issuance")
	}

	if _, err := h.reconcile("fn"); err != nil {
		t.Fatalf("recovery reconcile: %v", err)
	}
	h.assertConsistent("fn", []string{"fn.local"})
}

// 8. Issuance failure while a VALID old cert exists: the old cert is retained
// and Ready stays True; recovery then rotates.
func TestIssuanceFailureRetainsValidOldCertificate(t *testing.T) {
	h := newHarness(t, epoch)
	spec := durSpec("rt-tls", 15*time.Minute, 9*time.Minute, "rt.local") // healthy 1m
	h.createCert("rt", spec)
	if _, err := h.reconcile("rt"); err != nil {
		t.Fatal(err)
	}
	old := h.assertConsistent("rt", []string{"rt.local"})

	// Move into the renewal window; the old cert is still valid but renewal starts.
	h.clock.advance(61 * time.Second)
	h.signer.failRemaining = 1
	if _, err := h.reconcile("rt"); err != nil {
		t.Fatal(err)
	}
	retained := leafOf(t, h.getSecret("rt-tls"))
	if retained.SerialNumber.Cmp(old.SerialNumber) != 0 {
		t.Fatal("failed renewal must not delete/replace the still-valid old certificate")
	}
	cr := h.getCert("rt")
	if c := condOf(cr, string(certv1alpha1.ConditionReady)); c == nil || c.Status != metav1.ConditionTrue {
		t.Fatalf("Ready must remain True with valid old cert: %+v", c)
	}
	if cr.Status.Serial != pki.SerialString(old.SerialNumber) {
		t.Fatal("status must continue to summarize the retained certificate")
	}

	if _, err := h.reconcile("rt"); err != nil {
		t.Fatal(err)
	}
	next := h.assertConsistent("rt", []string{"rt.local"})
	if next.SerialNumber.Cmp(old.SerialNumber) == 0 {
		t.Fatal("certificate should rotate once issuance recovers")
	}
}

// 9. Restart while issuance is failing: the new controller process reuses the
// persisted pending CSR/key (same public key) rather than minting a new one.
func TestRestartReusesPendingApplication(t *testing.T) {
	h := newHarness(t, epoch)
	h.createCert("rs", baseSpec("rs-tls", "rs.local"))
	h.signer.failRemaining = 1
	if _, err := h.reconcile("rs"); err != nil {
		t.Fatal(err)
	}
	pend1 := h.getSecret("rs-tls-pending")
	key1, err := pki.ParsePrivateKeyPEM(pend1.Data[corev1.TLSPrivateKeyKey])
	if err != nil {
		t.Fatal(err)
	}

	// Simulate controller restart (fresh in-memory state, same etcd storage).
	h.restart()
	// Keep failing once more, then succeed.
	h.signer.failRemaining = 1
	if _, err := h.reconcile("rs"); err != nil {
		t.Fatal(err)
	}
	pend2, ok := h.secretMaybe("rs-tls-pending")
	if !ok {
		t.Fatal("pending secret should still exist while issuance keeps failing")
	}
	key2, err := pki.ParsePrivateKeyPEM(pend2.Data[corev1.TLSPrivateKeyKey])
	if err != nil {
		t.Fatal(err)
	}
	if !spkiEqual(key1.Public(), key2.Public()) {
		t.Fatal("restart did not reuse the in-flight pending key (key was rotated)")
	}

	if _, err := h.reconcile("rs"); err != nil {
		t.Fatal(err)
	}
	leaf := h.assertConsistent("rs", []string{"rs.local"})
	// Issued cert must be the public counterpart of the originally persisted key.
	if !spkiEqual(leaf.PublicKey, key1.Public()) {
		t.Fatal("issued certificate did not use the reused pending key")
	}
}

// 10. Repeated reconcile WITHOUT restart reuses the same pending application.
func TestRepeatedReconcileReusesPending(t *testing.T) {
	h := newHarness(t, epoch)
	h.createCert("rp", baseSpec("rp-tls", "rp.local"))
	h.signer.failRemaining = 2
	if _, err := h.reconcile("rp"); err != nil {
		t.Fatal(err)
	}
	k1, _ := pki.ParsePrivateKeyPEM(h.getSecret("rp-tls-pending").Data[corev1.TLSPrivateKeyKey])
	if _, err := h.reconcile("rp"); err != nil {
		t.Fatal(err)
	}
	k2, _ := pki.ParsePrivateKeyPEM(h.getSecret("rp-tls-pending").Data[corev1.TLSPrivateKeyKey])
	if !spkiEqual(k1.Public(), k2.Public()) {
		t.Fatal("second attempt rotated the pending key instead of reusing it")
	}
	if h.signer.calls != 2 {
		t.Fatalf("signer calls = %d want 2", h.signer.calls)
	}
	if _, err := h.reconcile("rp"); err != nil {
		t.Fatal(err)
	}
	h.assertConsistent("rp", []string{"rp.local"})
}

// 11. Missing CA Secret: serving secret is left untouched and Ready reflects
// CAUnavailable rather than deleting anything.
func TestMissingCANeverTouchesServingSecret(t *testing.T) {
	h := newHarness(t, epoch)
	h.createCert("mc", baseSpec("mc-tls", "mc.local"))
	if _, err := h.reconcile("mc"); err != nil {
		t.Fatal(err)
	}
	good := h.assertConsistent("mc", []string{"mc.local"})

	caSec := h.getSecret(caSecretName)
	if err := h.c.Delete(h.ctx, caSec); err != nil {
		t.Fatal(err)
	}
	if _, err := h.reconcile("mc"); err != nil {
		t.Fatalf("missing CA must not be a hard error: %v", err)
	}
	retained := leafOf(t, h.getSecret("mc-tls"))
	if retained.SerialNumber.Cmp(good.SerialNumber) != 0 {
		t.Fatal("serving secret changed while CA was unavailable")
	}
}

// 12. Invalid spec is reported and no secret activity occurs.
func TestInvalidSpec(t *testing.T) {
	h := newHarness(t, epoch)
	h.createCert("bad", baseSpec("bad-tls", "bad.local"))
	cr := h.getCert("bad")
	cr.Spec.DNSNames = nil
	if err := h.c.Update(h.ctx, cr); err != nil {
		t.Fatal(err)
	}
	if _, err := h.reconcile("bad"); err != nil {
		t.Fatal(err)
	}
	if _, ok := h.secretMaybe("bad-tls"); ok {
		t.Fatal("no serving secret should exist for an invalid spec")
	}
	if c := condOf(h.getCert("bad"), string(certv1alpha1.ConditionReady)); c == nil || c.Status != metav1.ConditionFalse {
		t.Fatalf("Ready=False expected for invalid spec: %+v", c)
	}
}
