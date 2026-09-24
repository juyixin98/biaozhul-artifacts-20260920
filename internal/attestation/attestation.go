// Package attestation defines the build-attestation format, its Ed25519
// signing operation, and the trust-policy verification path.
//
// This is a deliberately small, self-contained custom format. It is NOT
// Sigstore: there is no transparency log, no certificate chain, no Rekor, no
// OIDC federation and no timestamp authority. Trust is anchored in a static
// policy file listing allowed builders, keys and source repositories.
package attestation

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"build-attestation/internal/canonical"
)

// PayloadType and StatementType fix the purpose of a signature. The signed
// message starts with a domain-separation prefix so a key used for other
// purposes cannot produce an attestation this service accepts (and vice
// versa).
const (
	PayloadType   = "application/vnd.example.build-attestation.v1"
	StatementType = "build-attestation/statement/v1"

	// DomainSeparationPrefix is mixed into every signed message.
	DomainSeparationPrefix = "BUILD_ATTESTATION/v1\n"
)

// Digest identifies an artifact by its digest, e.g.
// {"alg":"sha256","value":"<64 hex chars>"}.
type Digest struct {
	Alg   string `json:"alg"`
	Value string `json:"value"`
}

// Statement is the canonicalized, signed claim. Every field is part of the
// signed material: changing the artifact digest, source commit, parameters,
// builder identity or timing invalidates the signature.
type Statement struct {
	Type      string         `json:"_type"`
	BuilderID string         `json:"builderId"`
	KeyID     string         `json:"keyId"`
	Source    Source         `json:"source"`
	Subjects  []Digest       `json:"subjects"`
	Params    map[string]any `json:"params"`
	IssuedAt  string         `json:"issuedAt"`
	Nonce     string         `json:"nonce"`
}

// Source pins the provenance of the code that produced the artifacts.
type Source struct {
	Repository string `json:"repository"`
	Commit     string `json:"commit"`
}

// Envelope is the explicit JSON wrapper sent over HTTP. Payload is the exact
// canonical JSON encoding of the Statement; Signature is base64url(raw)
// Ed25519 over DomainSeparationPrefix + payload.
type Envelope struct {
	PayloadType string `json:"payloadType"`
	Payload     string `json:"payload"`
	Signature   string `json:"signature"`
}

// VerificationError carries a machine-readable code used as the HTTP verdict.
type VerificationError struct {
	Code    string
	Message string
}

func (e *VerificationError) Error() string { return e.Code + ": " + e.Message }

func verr(code, format string, args ...any) *VerificationError {
	return &VerificationError{Code: code, Message: fmt.Sprintf(format, args...)}
}

// Known verdict codes.
const (
	CodeMalformed        = "MALFORMED"
	CodeNonCanonical     = "NON_CANONICAL_PAYLOAD"
	CodeDuplicateKey     = "DUPLICATE_JSON_KEY"
	CodeBadSignature     = "BAD_SIGNATURE"
	CodeUnknownBuilder   = "UNKNOWN_BUILDER"
	CodeUnknownKey       = "UNKNOWN_KEY"
	CodeKeyNotYetValid   = "KEY_NOT_YET_VALID"
	CodeKeyExpired       = "KEY_EXPIRED"
	CodeKeyRevoked       = "KEY_REVOKED"
	CodeUntrustedSource  = "UNTRUSTED_SOURCE"
	CodeFreshness        = "ISSUED_AT_OUTSIDE_WINDOW"
	CodeCrossPurpose     = "CROSS_PURPOSE_SIGNATURE"
	CodeReplay           = "REPLAYED_NONCE"
	CodeStatementInvalid = "INVALID_STATEMENT"
)

var b64 = base64.RawURLEncoding

// SigningInput returns the exact bytes signed by an attestation key.
func SigningInput(canonicalPayload []byte) []byte {
	out := make([]byte, 0, len(DomainSeparationPrefix)+len(canonicalPayload))
	out = append(out, DomainSeparationPrefix...)
	out = append(out, canonicalPayload...)
	return out
}

// CanonicalizeStatement strictly validates the statement shape and returns its
// canonical JSON encoding.
func CanonicalizeStatement(st *Statement) ([]byte, *VerificationError) {
	if verr0 := validateStatementShape(st); verr0 != nil {
		return nil, verr0
	}
	raw, err := json.Marshal(st)
	if err != nil {
		return nil, verr(CodeMalformed, "encoding statement: %v", err)
	}
	v, err := canonical.Parse(raw)
	if err != nil {
		return nil, mapCanonicalError(err)
	}
	out, err := canonical.Marshal(v)
	if err != nil {
		return nil, verr(CodeNonCanonical, "%v", err)
	}
	return out, nil
}

// Sign produces an Envelope for a statement using the builder's Ed25519 key.
func Sign(st *Statement, key ed25519.PrivateKey) (*Envelope, *VerificationError) {
	payload, err := CanonicalizeStatement(st)
	if err != nil {
		return nil, err
	}
	sig := ed25519.Sign(key, SigningInput(payload))
	return &Envelope{
		PayloadType: PayloadType,
		Payload:     b64.EncodeToString(payload),
		Signature:   b64.EncodeToString(sig),
	}, nil
}

// SignWithPrefix is exposed for tests to demonstrate cross-purpose rejection:
// it signs the canonical payload with a different domain prefix.
func SignWithPrefix(st *Statement, key ed25519.PrivateKey, prefix string) (*Envelope, *VerificationError) {
	payload, err := CanonicalizeStatement(st)
	if err != nil {
		return nil, err
	}
	msg := append([]byte(prefix), payload...)
	sig := ed25519.Sign(key, msg)
	return &Envelope{
		PayloadType: PayloadType,
		Payload:     b64.EncodeToString(payload),
		Signature:   b64.EncodeToString(sig),
	}, nil
}

func mapCanonicalError(err error) *VerificationError {
	var dup *canonical.DuplicateKeyError
	if errors.As(err, &dup) {
		return verr(CodeDuplicateKey, "%v", err)
	}
	return verr(CodeNonCanonical, "%v", err)
}

// ParseStatement decodes canonical payload bytes into a Statement, accepting
// only canonical JSON and no unknown fields.
func ParseStatement(payload []byte) (*Statement, *VerificationError) {
	v, err := canonical.Parse(payload)
	if err != nil {
		return nil, mapCanonicalError(err)
	}
	if _, ok := v.(map[string]any); !ok {
		return nil, verr(CodeMalformed, "payload must be a JSON object")
	}
	dec := json.NewDecoder(bytes.NewReader(payload))
	dec.DisallowUnknownFields()
	var st Statement
	if err := dec.Decode(&st); err != nil {
		return nil, verr(CodeStatementInvalid, "invalid statement: %v", cleanJSONError(err))
	}
	if err := validateStatementShape(&st); err != nil {
		return nil, err
	}
	return &st, nil
}

func cleanJSONError(err error) string {
	s := err.Error()
	s = strings.ReplaceAll(s, "json: ", "")
	return s
}

// ParseEnvelope strictly decodes the outer wrapper, rejecting duplicate or
// unknown fields.
func ParseEnvelope(data []byte) (*Envelope, *VerificationError) {
	v, err := canonical.Parse(data)
	if err != nil {
		return nil, mapCanonicalError(err)
	}
	if _, ok := v.(map[string]any); !ok {
		return nil, verr(CodeMalformed, "envelope must be a JSON object")
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var env Envelope
	if err := dec.Decode(&env); err != nil {
		return nil, verr(CodeMalformed, "invalid envelope: %v", cleanJSONError(err))
	}
	if env.PayloadType == "" || env.Payload == "" || env.Signature == "" {
		return nil, verr(CodeMalformed, "envelope requires payloadType, payload and signature")
	}
	return &env, nil
}
