// Package model defines the wire types for the offline mirror admission
// service. Nothing here defaults an absent security-relevant field to a
// permissive value: the policy engine treats missing evidence as unknown.
package model

import "encoding/json"

// AdmissionRequest is the POST /v1/admission/evaluate payload.
type AdmissionRequest struct {
	// ImageConfig is the raw OCI image configuration JSON. The engine hashes
	// the EXACT bytes received, so callers must not re-marshal/sort the
	// document before sending it.
	ImageConfig json.RawMessage `json:"image_config"`

	// SBOM is the raw SBOM JSON document.
	SBOM json.RawMessage `json:"sbom"`

	// Attestation is the signed verifier-result envelope produced by the
	// local tester (cmd/verifier). Optional at the wire level; a missing
	// attestation yields an UNKNOWN rule finding, never a pass.
	Attestation json.RawMessage `json:"attestation,omitempty"`

	// Exemptions are signed exemption envelopes. Each is independently
	// signature-verified; any invalid envelope fails the whole request.
	Exemptions []json.RawMessage `json:"exemptions,omitempty"`

	// AllowlistVersion pins which allowlist revision the caller expects.
	// Must match the server's loaded allowlist version.
	AllowlistVersion string `json:"allowlist_version"`
}

// SBOMContent is the small slice of the SBOM the policy reads.
type SBOMContent struct {
	ImageDigest     string `json:"image_digest"`
	BaseImageDigest string `json:"base_image_digest"`
	Distro          string `json:"distro,omitempty"`
}

// VerifierPayload is the body the local tester signs. It binds its verdict to
// exact content digests; signature validity plus digest equality is what makes
// the attestation evidence rather than assertion.
type VerifierPayload struct {
	Kind          string   `json:"kind"` // always "mirrorad.verifier_result/v1"
	SignerKeyID   string   `json:"signer_key_id"`
	ImageDigest   string   `json:"image_digest"`
	SBOMDigest    string   `json:"sbom_digest"`
	Result        string   `json:"result"` // "pass" | "fail"
	Checks        []string `json:"checks"`
	GeneratedAt   string   `json:"generated_at"`
	VerifierBuild string   `json:"verifier_build,omitempty"`
}

// ExemptionPayload binds a single waiver to one image digest, one rule and an
// absolute expiry. There is deliberately no wildcard on either dimension.
type ExemptionPayload struct {
	Kind        string `json:"kind"` // always "mirrorad.exemption/v1"
	ID          string `json:"id"`
	SignerKeyID string `json:"signer_key_id"`
	ImageDigest string `json:"image_digest"`
	Rule        string `json:"rule"`
	NotAfter    string `json:"not_after"` // RFC3339; boundary == expiry
	Reason      string `json:"reason"`
	GrantedAt   string `json:"granted_at"`
}

// SignedEnvelope is the canonical on-disk/on-wire signature container.
// Signature is base64(RFC 8032 Ed25519) over CanonicalJSON(Payload),
// where Payload itself is carried as raw JSON and canonicalised for
// verification exactly as the signer canonicalised it.
type SignedEnvelope struct {
	PayloadType string          `json:"payload_type"`
	Payload     json.RawMessage `json:"payload"`
	Signature   string          `json:"signature"` // base64, 64 bytes for Ed25519
}

// Finding is one rule's result with its human-readable rationale.
type Finding struct {
	Rule       string   `json:"rule"`
	Status     string   `json:"status"` // allow | deny | unknown | exempt
	Reason     string   `json:"reason"`
	Exemptions []string `json:"exemptions"`
}

// UnusedExemption explains why a presented (validly signed) exemption did not
// downgrade anything.
type UnusedExemption struct {
	ID     string `json:"id"`
	Rule   string `json:"rule"`
	Reason string `json:"reason"`
}

// Report is the immutable per-evaluation result. Reports are append-only:
// re-evaluation always creates a new report id and never overwrites an old
// one, even for the same digest.
type Report struct {
	ID               string            `json:"id"`
	CreatedAt        string            `json:"created_at"`
	Decision         string            `json:"decision"` // ALLOW | DENY | UNKNOWN
	PolicyVersion    string            `json:"policy_version"`
	PolicySHA256     string            `json:"policy_sha256"`
	AllowlistVersion string            `json:"allowlist_version"`
	ImageDigest      string            `json:"image_digest"`
	SBOMDigest       string            `json:"sbom_digest"`
	Findings         []Finding         `json:"findings"`
	UnusedExemptions []UnusedExemption `json:"unused_exemptions"`
	Attestation      *VerifierPayload  `json:"attestation,omitempty"`
}

// PolicyMeta describes the frozen policy+allowlist the server is running.
type PolicyMeta struct {
	PolicyVersion     string   `json:"policy_version"`
	PolicySHA256      string   `json:"policy_sha256"`
	AllowlistVersion  string   `json:"allowlist_version"`
	AllowedBaseImages []string `json:"allowed_base_images"`
	Rules             []string `json:"rules"`
}
