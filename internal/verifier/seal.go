package verifier

import (
	"crypto/ed25519"
	"encoding/json"
	"time"

	"mirror-admission/internal/crypto/sig"
	"mirror-admission/internal/model"
)

func validRFC3339(s string) error {
	_, err := time.Parse(time.RFC3339, s)
	return err
}

// SealAttestation is the signing-side helper used by cmd/verifier. It signs
// the canonical payload and returns the wire envelope. Production admission
// never calls this; it exists in the same module so test and tooling code
// exercises exactly the same canonicalisation as verification.
func SealAttestation(priv ed25519.PrivateKey, p model.VerifierPayload) (json.RawMessage, error) {
	return seal(priv, AttestationKind, p)
}

// SealExemption signs an exemption with the authority key.
func SealExemption(priv ed25519.PrivateKey, p model.ExemptionPayload) (json.RawMessage, error) {
	return seal(priv, ExemptionKind, p)
}

func seal(priv ed25519.PrivateKey, typ string, payload any) (json.RawMessage, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	signature, _, err := sig.Sign(priv, body)
	if err != nil {
		return nil, err
	}
	env := model.SignedEnvelope{
		PayloadType: typ,
		Payload:     body,
		Signature:   signature,
	}
	out, err := json.MarshalIndent(env, "", "  ")
	if err != nil {
		return nil, err
	}
	return out, nil
}
