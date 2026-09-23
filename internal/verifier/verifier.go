// Package verifier performs the REAL signature and binding checks for the two
// signed evidence types the admission engine accepts:
//
//   - verifier attestations (produced by the local tester that ran the image
//     through its offline checks), and
//   - policy exemptions (granted by the offline policy authority).
//
// Both are Ed25519-signed envelopes. A signature error is never downgraded:
// it becomes a DENY-level finding for attestations and a hard request error
// for exemptions (you cannot smuggle an unverified waiver into evaluation).
package verifier

import (
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"

	"mirror-admission/internal/crypto/sig"
	"mirror-admission/internal/model"
)

const (
	AttestationKind = "mirrorad.verifier_result/v1"
	ExemptionKind   = "mirrorad.exemption/v1"
)

// ErrInvalidEnvelope wraps every signature/structural failure so the HTTP
// layer can distinguish client-presented invalid evidence from server bugs.
var ErrInvalidEnvelope = errors.New("invalid signed envelope")

type errInvalid struct{ msg string }

func (e *errInvalid) Error() string { return e.msg }
func (e *errInvalid) Unwrap() error { return ErrInvalidEnvelope }

func fail(format string, args ...any) error {
	return &errInvalid{msg: fmt.Sprintf(format, args...)}
}

// Keys are the two trust roots loaded at startup. They must be different
// keys: a tester key must never be able to mint exemptions.
type Keys struct {
	Verifier  ed25519.PublicKey
	Exemption ed25519.PublicKey
}

func keyIDOrDie(pub ed25519.PublicKey) string {
	id, err := sig.KeyID(pub)
	if err != nil {
		panic(err)
	}
	return id
}

// parseEnvelope does structure-level parsing only.
func parseEnvelope(raw json.RawMessage) (*model.SignedEnvelope, error) {
	var env model.SignedEnvelope
	if err := json.Unmarshal(raw, &env); err != nil {
		return nil, fail("envelope is not valid JSON: %v", err)
	}
	if env.Payload == nil || len(env.Payload) == 0 {
		return nil, fail("envelope has no payload")
	}
	if env.Signature == "" {
		return nil, fail("envelope has no signature")
	}
	return &env, nil
}

// VerifyAttestation checks the tester signature and every content binding.
// On success the parsed, authentic payload is returned.
func VerifyAttestation(raw json.RawMessage, k Keys, imageDigest, sbomDigest string) (*model.VerifierPayload, error) {
	env, err := parseEnvelope(raw)
	if err != nil {
		return nil, err
	}
	if env.PayloadType != AttestationKind {
		return nil, fail("payload_type %q, want %q", env.PayloadType, AttestationKind)
	}
	if err := sig.Verify(k.Verifier, env.Payload, env.Signature); err != nil {
		return nil, fail("attestation signature verification failed: %v", err)
	}
	var p model.VerifierPayload
	if err := json.Unmarshal(env.Payload, &p); err != nil {
		return nil, fail("attestation payload parse: %v", err)
	}
	if p.Kind != AttestationKind {
		return nil, fail("attestation payload kind %q, want %q", p.Kind, AttestationKind)
	}
	// The payload must identify the very key that signed it.
	wantID := keyIDOrDie(k.Verifier)
	if p.SignerKeyID != wantID {
		return nil, fail("attestation signer_key_id %q does not match trusted verifier key %q", p.SignerKeyID, wantID)
	}
	if p.ImageDigest == "" || p.SBOMDigest == "" {
		return nil, fail("attestation payload omits image_digest/sbom_digest binding")
	}
	if p.ImageDigest != imageDigest {
		return nil, fail("attestation bound to %s but evaluated image is %s (label drift / re-tag does not rebind evidence)", p.ImageDigest, imageDigest)
	}
	if p.SBOMDigest != sbomDigest {
		return nil, fail("attestation bound to SBOM %s but evaluated SBOM is %s", p.SBOMDigest, sbomDigest)
	}
	if p.Result != "pass" && p.Result != "fail" {
		return nil, fail("attestation result %q is neither pass nor fail", p.Result)
	}
	return &p, nil
}

// VerifyExemption checks an exemption envelope with the exemption authority
// key, enforces the kind/key-id binding and validates the expiry timestamp.
// Digest/rule scope is enforced by Rego; here we make sure the document is
// structurally authentic.
func VerifyExemption(raw json.RawMessage, k Keys) (*model.ExemptionPayload, error) {
	env, err := parseEnvelope(raw)
	if err != nil {
		return nil, err
	}
	if env.PayloadType != ExemptionKind {
		return nil, fail("exemption payload_type %q, want %q", env.PayloadType, ExemptionKind)
	}
	if err := sig.Verify(k.Exemption, env.Payload, env.Signature); err != nil {
		return nil, fail("exemption signature verification failed: %v", err)
	}
	var p model.ExemptionPayload
	if err := json.Unmarshal(env.Payload, &p); err != nil {
		return nil, fail("exemption payload parse: %v", err)
	}
	if p.Kind != ExemptionKind {
		return nil, fail("exemption payload kind %q, want %q", p.Kind, ExemptionKind)
	}
	wantID := keyIDOrDie(k.Exemption)
	if p.SignerKeyID != wantID {
		return nil, fail("exemption signer_key_id %q does not match trusted exemption key %q", p.SignerKeyID, wantID)
	}
	if p.ID == "" {
		return nil, fail("exemption has no id")
	}
	if p.ImageDigest == "" || !looksLikeDigest(p.ImageDigest) {
		return nil, fail("exemption %s does not bind a concrete image digest", p.ID)
	}
	if p.Rule == "" {
		return nil, fail("exemption %s does not name a specific rule", p.ID)
	}
	if p.NotAfter == "" {
		return nil, fail("exemption %s has no not_after expiry", p.ID)
	}
	if err := validRFC3339(p.NotAfter); err != nil {
		return nil, fail("exemption %s not_after: %v", p.ID, err)
	}
	return &p, nil
}

func looksLikeDigest(s string) bool {
	// sha256:<64 lowercase hex>
	const prefix = "sha256:"
	if len(s) != len(prefix)+64 || s[:len(prefix)] != prefix {
		return false
	}
	for _, c := range s[len(prefix):] {
		switch {
		case c >= '0' && c <= '9', c >= 'a' && c <= 'f':
		default:
			return false
		}
	}
	return true
}
