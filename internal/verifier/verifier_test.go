package verifier_test

import (
	"crypto/ed25519"
	"crypto/rand"
	"strings"
	"testing"

	"mirror-admission/internal/crypto/digest"
	"mirror-admission/internal/crypto/sig"
	"mirror-admission/internal/model"
	"mirror-admission/internal/testkit"
	"mirror-admission/internal/verifier"
)

func TestAttestationRoundTripAndWrongBinding(t *testing.T) {
	k := testkit.GenerateKeys(t)
	cfg := []byte(`{"architecture":"amd64","config":{"User":"10001","Privileged":false}}`)
	sbomDoc := []byte(`{"image_digest":"sha256:1111111111111111111111111111111111111111111111111111111111111111"}`)

	// Canonical digests used for both signing and verification.
	imageD, _, err := digest.ParseOCI(cfg)
	if err != nil {
		t.Fatal(err)
	}
	sbomD, _, err := digest.ParseSBOM(sbomDoc)
	if err != nil {
		t.Fatal(err)
	}

	payload := model.VerifierPayload{
		Kind: verifier.AttestationKind, SignerKeyID: k.VerifierID,
		ImageDigest: imageD, SBOMDigest: sbomD, Result: "pass",
		Checks: []string{"x"},
	}
	env, err := verifier.SealAttestation(k.VerifierPriv, payload)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := verifier.VerifyAttestation(env, k.Trusted(), imageD, sbomD); err != nil {
		t.Fatalf("genuine attestation rejected: %v", err)
	}

	// Same envelope checked against a different image digest must fail.
	if _, err := verifier.VerifyAttestation(env, k.Trusted(),
		"sha256:"+strings.Repeat("9", 64), sbomD); err == nil {
		t.Fatal("binding to wrong image digest accepted")
	}
}

func TestExemptionStructuralValidation(t *testing.T) {
	k := testkit.GenerateKeys(t)
	good := model.ExemptionPayload{
		Kind: verifier.ExemptionKind, ID: "ex-1", SignerKeyID: k.ExemptID,
		ImageDigest: "sha256:" + strings.Repeat("a", 64), Rule: "no_root_user",
		NotAfter: "2026-10-01T00:00:00Z",
	}
	env, err := verifier.SealExemption(k.ExemptPriv, good)
	if err != nil {
		t.Fatal(err)
	}
	got, err := verifier.VerifyExemption(env, k.Trusted())
	if err != nil {
		t.Fatalf("valid exemption rejected: %v", err)
	}
	if got.ID != "ex-1" {
		t.Fatal("exemption id mismatch")
	}

	bad := good
	bad.ImageDigest = "not-a-digest"
	badEnv, _ := verifier.SealExemption(k.ExemptPriv, bad)
	if _, err := verifier.VerifyExemption(badEnv, k.Trusted()); err == nil {
		t.Fatal("exemption without concrete digest accepted")
	}

	badTime := good
	badTime.NotAfter = "next tuesday"
	badEnv2, _ := verifier.SealExemption(k.ExemptPriv, badTime)
	if _, err := verifier.VerifyExemption(badEnv2, k.Trusted()); err == nil {
		t.Fatal("exemption with unparseable expiry accepted")
	}
}

func TestWrongSignerKeyIDRejected(t *testing.T) {
	k := testkit.GenerateKeys(t)
	otherPub, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	otherID, _ := sig.KeyID(otherPub)

	payload := model.ExemptionPayload{
		Kind: verifier.ExemptionKind, ID: "ex", SignerKeyID: otherID, // claims to be someone else
		ImageDigest: "sha256:" + strings.Repeat("a", 64),
		Rule:        "no_root_user", NotAfter: "2026-10-01T00:00:00Z",
	}
	// Signed by the real authority key but claims a different signer id.
	env, err := verifier.SealExemption(k.ExemptPriv, payload)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := verifier.VerifyExemption(env, k.Trusted()); err == nil {
		t.Fatal("exemption with mismatched signer_key_id accepted")
	}
}

func TestGarbageEnvelopesRejected(t *testing.T) {
	k := testkit.GenerateKeys(t)
	for _, raw := range [][]byte{
		[]byte("not json"),
		[]byte(`{"payload_type":"x","payload":{}}`),                      // no signature
		[]byte(`{"payload_type":"x","signature":"AAAA","payload":"{}"}`), // bad sig length
	} {
		if _, err := verifier.VerifyExemption(raw, k.Trusted()); err == nil {
			t.Fatalf("garbage envelope accepted: %s", raw)
		}
	}
}
