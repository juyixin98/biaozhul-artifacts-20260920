// Package provenance holds the domain model for build input provenance:
// sources, declared build actions/tools, artifacts, and the attestation
// records that bind an artifact to its source digests, tool digest, and
// upstream artifacts.
package provenance

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
)

// Digest is a lowercase hex SHA-256 content digest, prefixed with "sha256:".
type Digest string

// DigestBytes computes sha256(b) and returns it as a Digest.
func DigestBytes(b []byte) Digest {
	sum := sha256.Sum256(b)
	return Digest("sha256:" + hex.EncodeToString(sum[:]))
}

// ParseDigest validates and normalizes a digest string.
func ParseDigest(s string) (Digest, error) {
	parts := strings.SplitN(s, ":", 2)
	if len(parts) != 2 || parts[0] != "sha256" || len(parts[1]) != hex.EncodedLen(sha256.Size) {
		return "", fmt.Errorf("invalid digest %q: expected sha256:%d hex chars", s, hex.EncodedLen(sha256.Size))
	}
	if _, err := hex.DecodeString(parts[1]); err != nil {
		return "", fmt.Errorf("invalid digest %q: %w", s, err)
	}
	return Digest(strings.ToLower(s)), nil
}

// Hex returns the bare 64-char hex portion.
func (d Digest) Hex() string {
	return strings.TrimPrefix(string(d), "sha256:")
}

func (d Digest) String() string { return string(d) }

// Short returns a short identifier suitable for log lines.
func (d Digest) Short() string {
	h := d.Hex()
	if len(h) >= 12 {
		return h[:12]
	}
	return h
}

// Source is a registered user-supplied input file (source or fixture data).
// Content lives in the content-addressed source store; only metadata is kept
// in the registry.
type Source struct {
	Path      string `json:"path"`      // logical, forward-slash path identifying the source
	Digest    Digest `json:"digest"`    // sha256 of the registered bytes
	Size      int64  `json:"size"`      // byte size
	CreatedAt string `json:"createdAt"` // RFC3339Nano timestamp
}

// Tool is the declared, immutable build action definition. Its digest
// summarizes everything that could deterministically affect execution except
// the action's bound inputs: the command argv, static environment, the
// interpreter binary digest, and the fixture script digest.
type Tool struct {
	Name         string            `json:"name"`
	Command      []string          `json:"command"`               // argv; command[1], when present, must be the fixture script
	Env          map[string]string `json:"env,omitempty"`         // static environment added on top of the executor's base env
	Interpreter  string            `json:"interpreter,omitempty"` // resolved binary path that actually executes
	Script       string            `json:"script,omitempty"`      // resolved fixture script path
	Digest       Digest            `json:"digest"`                // digest of the canonical definition + file digests
	RegisteredAt string            `json:"registeredAt"`
}

// ToolDefinition is what a client submits to register a build action.
type ToolDefinition struct {
	Name    string            `json:"name"`
	Command []string          `json:"command"`
	Env     map[string]string `json:"env,omitempty"`
}

// Binding binds one input slot of an action to a source or a prior artifact.
// Exactly one of SourcePath / ArtifactID is set.
type Binding struct {
	Slot       string `json:"slot"`
	SourcePath string `json:"sourcePath,omitempty"`
	ArtifactID string `json:"artifactId,omitempty"`
}

// ActionRequest declares one build action invocation.
type ActionRequest struct {
	Tool    string    `json:"tool"`
	Inputs  []Binding `json:"inputs"`
	Outputs []string  `json:"outputs"` // output slot names; files appear as $OUT_<SLOT>
}

// InputRef is the resolved, materialized input recorded in an attestation.
type InputRef struct {
	Slot       string `json:"slot"`
	Kind       string `json:"kind"` // "source" | "artifact"
	Path       string `json:"path,omitempty"`
	ArtifactID string `json:"artifactId,omitempty"`
	Digest     Digest `json:"digest"`
}

// Artifact is a build output recorded by the service. Content lives in the
// content-addressed artifact cache, separate from execution work directories.
type Artifact struct {
	ID         string `json:"id"` // art_<sha256(action inputs/output digest)>[:16]
	ActionID   string `json:"actionId"`
	Name       string `json:"name"` // output slot name
	Digest     Digest `json:"digest"`
	Size       int64  `json:"size"`
	CreatedAt  string `json:"createdAt"`
	RecordHash string `json:"recordHash"` // hash of the attestation record describing this artifact
}

// Record is an attestation: the provenance statement for one artifact.
//
// It binds the artifact content to (a) the exact source/artifact inputs with
// their digests, (b) the tool definition digest, and (c) the attestation
// hashes of every immediate upstream record. This is a transitive provenance
// chain; Prev additionally links records into an append-only log chain.
type Record struct {
	RecordHash string     `json:"recordHash"`
	Prev       string     `json:"prev"`
	ActionID   string     `json:"actionId"`
	ArtifactID string     `json:"artifactId"`
	ToolName   string     `json:"toolName"`
	ToolDigest Digest     `json:"toolDigest"`
	Inputs     []InputRef `json:"inputs"`
	OutputName string     `json:"outputName"`
	OutputSize int64      `json:"outputSize"`
	// OutputDigest binds the attestation content to the exact artifact bytes.
	OutputDigest Digest            `json:"outputDigest"`
	CreatedAt    string            `json:"createdAt"`
	Sig          string            `json:"sig"`       // HMAC-SHA256(hex(recordHash)), hex
	Upstreams    map[string]string `json:"upstreams"` // artifactID -> upstream record hash
}
