// Package testkit provides genuinely-generated keys and real signing for
// tests. Nothing here stubs cryptography: envelopes produced through testkit
// are verified through the production verification path.
package testkit

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"testing"
	"time"

	"mirror-admission/internal/crypto/digest"
	"mirror-admission/internal/crypto/sig"
	"mirror-admission/internal/model"
	"mirror-admission/internal/policy"
	"mirror-admission/internal/verifier"
)

// Keys bundles generated test trust material.
type Keys struct {
	VerifierPub  ed25519.PublicKey
	VerifierPriv ed25519.PrivateKey
	ExemptPub    ed25519.PublicKey
	ExemptPriv   ed25519.PrivateKey
	VerifierID   string
	ExemptID     string
}

// GenerateKeys returns two distinct Ed25519 keypairs.
func GenerateKeys(t *testing.T) Keys {
	t.Helper()
	vPub, vPriv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	ePub, ePriv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	vID, err := sig.KeyID(vPub)
	if err != nil {
		t.Fatal(err)
	}
	eID, err := sig.KeyID(ePub)
	if err != nil {
		t.Fatal(err)
	}
	return Keys{vPub, vPriv, ePub, ePriv, vID, eID}
}

// Trusted returns the verifier.Keys the server loads.
func (k Keys) Trusted() verifier.Keys {
	return verifier.Keys{Verifier: k.VerifierPub, Exemption: k.ExemptPub}
}

// AllowBase is a valid allowlisted base digest.
const AllowBase = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

// DenyBase is a validly-shaped but unlisted base digest.
const DenyBase = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

// Allowlist returns the frozen test allowlist matching the examples bundle.
func Allowlist() policy.Allowlist {
	return policy.Allowlist{Version: "allowlist-test-1", AllowedBaseImages: []string{AllowBase}}
}

// Config builds an OCI image config from a config-section map.
func Config(section map[string]any) map[string]any {
	return map[string]any{
		"architecture": "amd64",
		"os":           "linux",
		"created":      "2026-09-20T00:00:00Z",
		"config":       section,
		"rootfs":       map[string]any{"type": "layers", "diff_ids": []string{}},
	}
}

// MustMarshal returns indented JSON (indentation deliberately varies from
// canonical form, exercising digest canonicalisation).
func MustMarshal(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// SBOM builds an SBOM whose image digest matches the config bytes.
func SBOM(t *testing.T, cfgBytes []byte, baseDigest string) ([]byte, string) {
	t.Helper()
	imageD, _, err := digest.ParseOCI(cfgBytes)
	if err != nil {
		t.Fatal(err)
	}
	doc := map[string]any{
		"image_digest":      imageD,
		"base_image_digest": baseDigest,
		"distro":            "test",
	}
	b := MustMarshal(t, doc)
	sbomD, _, err := digest.ParseSBOM(b)
	if err != nil {
		t.Fatal(err)
	}
	return b, sbomD
}

// Attest seals a tester result envelope over the actual config/SBOM bytes.
func Attest(t *testing.T, k Keys, cfgBytes, sbomBytes []byte, result string) json.RawMessage {
	t.Helper()
	imageD, _, err := digest.ParseOCI(cfgBytes)
	if err != nil {
		t.Fatal(err)
	}
	sbomD, _, err := digest.ParseSBOM(sbomBytes)
	if err != nil {
		t.Fatal(err)
	}
	p := model.VerifierPayload{
		Kind:        verifier.AttestationKind,
		SignerKeyID: k.VerifierID,
		ImageDigest: imageD,
		SBOMDigest:  sbomD,
		Result:      result,
		Checks:      []string{"root", "privileged", "baseimage"},
		GeneratedAt: time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC).Format(time.RFC3339),
	}
	env, err := verifier.SealAttestation(k.VerifierPriv, p)
	if err != nil {
		t.Fatal(err)
	}
	return env
}

// Exempt seals an exemption for one digest/rule/deadline.
func Exempt(t *testing.T, k Keys, id, imageDigest, rule, notAfter string) json.RawMessage {
	t.Helper()
	p := model.ExemptionPayload{
		Kind:        verifier.ExemptionKind,
		ID:          id,
		SignerKeyID: k.ExemptID,
		ImageDigest: imageDigest,
		Rule:        rule,
		NotAfter:    notAfter,
		Reason:      "test waiver",
		GrantedAt:   time.Date(2026, 9, 20, 9, 0, 0, 0, time.UTC).Format(time.RFC3339),
	}
	env, err := verifier.SealExemption(k.ExemptPriv, p)
	if err != nil {
		t.Fatal(err)
	}
	return env
}

// Request assembles the wire request body.
func Request(cfgBytes, sbomBytes, att []byte, exs [][]byte, allowVersion string) model.AdmissionRequest {
	req := model.AdmissionRequest{
		ImageConfig:      cfgBytes,
		SBOM:             sbomBytes,
		Attestation:      att,
		AllowlistVersion: allowVersion,
	}
	for _, e := range exs {
		req.Exemptions = append(req.Exemptions, e)
	}
	return req
}

// FixedClock implements service.Clock.
type FixedClock struct{ T time.Time }

func (c FixedClock) Now() time.Time { return c.T }
