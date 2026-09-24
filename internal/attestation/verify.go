package attestation

import (
	"bytes"
	"crypto/ed25519"
	"errors"
	"time"

	"build-attestation/internal/canonical"
)

// Verifier evaluates envelopes against a Policy.
type Verifier struct {
	policy    *Policy
	now       func() time.Time
	skew      time.Duration
	replay    *replayCache
	replayTTL time.Duration
}

// Config configures a Verifier.
type Config struct {
	Policy          *Policy
	Now             func() time.Time // defaults to time.Now (UTC)
	FreshnessWindow time.Duration    // defaults to 5 minutes
	ReplayCacheSize int              // defaults to 100000 nonces
}

// NewVerifier constructs a verifier from a config.
func NewVerifier(cfg Config) (*Verifier, error) {
	if cfg.Policy == nil {
		return nil, errors.New("verifier requires a policy")
	}
	now := cfg.Now
	if now == nil {
		now = func() time.Time { return time.Now().UTC() }
	}
	skew := cfg.FreshnessWindow
	if skew <= 0 {
		skew = 5 * time.Minute
	}
	return &Verifier{
		policy:    cfg.Policy,
		now:       now,
		skew:      skew,
		replay:    newReplayCache(cfg.ReplayCacheSize),
		replayTTL: 2*skew + time.Minute,
	}, nil
}

// Result describes a successful verification.
type Result struct {
	BuilderID  string    `json:"builderId"`
	KeyID      string    `json:"keyId"`
	Repository string    `json:"repository"`
	Commit     string    `json:"commit"`
	Subjects   []Digest  `json:"subjects"`
	IssuedAt   time.Time `json:"issuedAt"`
	Nonce      string    `json:"nonce"`
}

// Verify runs the full check:
//
//  1. strict envelope parse (no duplicate/unknown fields)
//  2. base64url decode and exact canonical-form check of the payload
//  3. statement shape validation (types, digest and commit formats)
//  4. payloadType and statement _type pin the signature purpose
//  5. Ed25519 verification with the domain-separation prefix
//  6. builder must be trusted and the key must belong to that builder
//  7. key rotation window / revocation at the issuing time
//  8. source repository must be policy-allowed
//  9. issuance timestamp within the freshness window
//  10. nonce not previously consumed (replay rejection)
func (v *Verifier) Verify(rawEnvelope []byte) (*Result, *VerificationError) {
	env, verr0 := ParseEnvelope(rawEnvelope)
	if verr0 != nil {
		return nil, verr0
	}
	if env.PayloadType != PayloadType {
		return nil, verr(CodeCrossPurpose, "payloadType must be %q, got %q", PayloadType, env.PayloadType)
	}

	payload, err := b64.DecodeString(env.Payload)
	if err != nil {
		return nil, verr(CodeMalformed, "payload is not base64url: %v", err)
	}
	sig, err := b64.DecodeString(env.Signature)
	if err != nil {
		return nil, verr(CodeMalformed, "signature is not base64url: %v", err)
	}
	if len(sig) != ed25519.SignatureSize {
		return nil, verr(CodeMalformed, "signature must be %d bytes, got %d", ed25519.SignatureSize, len(sig))
	}

	// The wire payload must be the exact canonical encoding. Parse+re-encode
	// and compare byte-for-byte so whitespace, key order or number shape
	// changes cannot hide behind a valid signature over a different blob.
	parsed, cErr := canonical.Parse(payload)
	if cErr != nil {
		return nil, mapCanonicalError(cErr)
	}
	recoded, merr := canonical.Marshal(parsed)
	if merr != nil {
		return nil, verr(CodeMalformed, "re-encoding payload: %v", merr)
	}
	if !bytes.Equal(recoded, payload) {
		return nil, verr(CodeNonCanonical, "payload bytes are not the canonical encoding; re-encode before signing")
	}

	st, stErr := ParseStatement(payload)
	if stErr != nil {
		return nil, stErr
	}

	builder := v.policy.LookupBuilder(st.BuilderID)
	if builder == nil {
		return nil, verr(CodeUnknownBuilder, "builder %q is not in the trust policy", st.BuilderID)
	}
	owner, key := v.policy.LookupKey(st.KeyID)
	if key == nil {
		return nil, verr(CodeUnknownKey, "keyId %q is not in the trust policy", st.KeyID)
	}
	if owner.ID != builder.ID {
		// A valid key from another builder cannot vouch for this builder id.
		return nil, verr(CodeUnknownKey, "key %q is not registered to builder %q", st.KeyID, st.BuilderID)
	}

	issued, _ := time.Parse(time.RFC3339, st.IssuedAt)
	if ks := keyStatusAt(key, issued); ks != nil {
		return nil, ks
	}

	if !v.policy.SourceAllowed(st.Source.Repository) {
		return nil, verr(CodeUntrustedSource, "source %q is not under an allowedSourcePrefix", st.Source.Repository)
	}

	// Signature check only after policy has identified the right key: this
	// binds digest, commit, params, builder identity, timing and nonce in one
	// Ed25519 verification.
	if !ed25519.Verify(key.pub, SigningInput(payload), sig) {
		return nil, verr(CodeBadSignature, "Ed25519 signature verification failed")
	}

	now := v.now()
	if delta := now.Sub(issued); delta > v.skew || delta < -v.skew {
		return nil, verr(CodeFreshness, "issuedAt %s is outside the ±%s acceptance window (now %s)",
			st.IssuedAt, v.skew, now.Format(time.RFC3339))
	}

	// Replay is consumed last: invalid attestations must not poison the cache.
	if rerr := v.replay.checkAndRemember(st.Nonce, now, v.replayTTL); rerr != nil {
		return nil, rerr
	}

	return &Result{
		BuilderID:  st.BuilderID,
		KeyID:      st.KeyID,
		Repository: st.Source.Repository,
		Commit:     st.Source.Commit,
		Subjects:   st.Subjects,
		IssuedAt:   issued,
		Nonce:      st.Nonce,
	}, nil
}
