// Package provenance defines the provenance record attached to every built
// artifact and the cryptography around it.
//
// A record binds, for one action execution:
//   - the exact tool command and its digest,
//   - every declared source file and its digest,
//   - every consumed upstream artifact (action id + output digest),
//   - every produced output and its digest,
//   - a deterministic fingerprint over the complete input set,
//   - a creation timestamp.
//
// Records are serialized as canonical JSON (fixed struct field order, no
// insignificant whitespace), hashed, and signed with HMAC-SHA256 using the
// service key. The record id equals the hash of its canonical body, so a
// stored file whose body no longer hashes to its name has been tampered
// with, and a changed body invalidates the signature.
package provenance

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"

	"bis/internal/digest"
)

// ToolRef identifies the exact executable of an action.
type ToolRef struct {
	Path   string        `json:"path"`
	Args   []string      `json:"args"`
	Digest digest.Digest `json:"digest"`
}

// SourceRef identifies one declared source input.
type SourceRef struct {
	Path   string        `json:"path"`
	Digest digest.Digest `json:"digest"`
}

// UpstreamRef identifies one consumed artifact of an upstream action.
type UpstreamRef struct {
	ActionID string        `json:"action_id"`
	Output   string        `json:"output"`
	Digest   digest.Digest `json:"digest"`
}

// OutputRef identifies one produced artifact.
type OutputRef struct {
	Path   string        `json:"path"`
	Digest digest.Digest `json:"digest"`
	Bytes  int64         `json:"bytes"`
}

// Record is the full provenance attestation for one action execution.
type Record struct {
	Schema           int           `json:"schema"`
	Project          string        `json:"project"`
	ActionID         string        `json:"action_id"`
	Tool             ToolRef       `json:"tool"`
	Sources          []SourceRef   `json:"sources"`
	Upstreams        []UpstreamRef `json:"upstreams"`
	Outputs          []OutputRef   `json:"outputs"`
	InputFingerprint digest.Digest `json:"input_fingerprint"`
	CreatedAt        string        `json:"created_at"`
	Sig              string        `json:"sig"`
}

// CanonicalVersion is bumped whenever the record layout changes.
const CanonicalVersion = 1

// unsigned returns a copy with the signature field blanked. Signing and
// hashing always operate on this unsigned body.
func (r *Record) unsigned() Record {
	c := *r
	c.Sig = ""
	return c
}

// CanonicalBytes returns the canonical serialization of the unsigned body.
func CanonicalBytes(r *Record) ([]byte, error) {
	return json.Marshal(r.unsigned())
}

// ContentID is the record id: sha256 of the canonical unsigned body.
func ContentID(r *Record) (digest.Digest, error) {
	b, err := CanonicalBytes(r)
	if err != nil {
		return digest.Digest{}, err
	}
	return digest.OfBytes(b), nil
}

// Sign sets the HMAC-SHA256 signature over the canonical unsigned body.
func (r *Record) Sign(key []byte) error {
	b, err := CanonicalBytes(r)
	if err != nil {
		return err
	}
	mac := hmac.New(sha256.New, key)
	mac.Write(b)
	r.Sig = hex.EncodeToString(mac.Sum(nil))
	return nil
}

// VerifySig checks the HMAC-SHA256 signature without mutating the record.
func (r *Record) VerifySig(key []byte) error {
	expected := r.Sig
	b, err := CanonicalBytes(r)
	if err != nil {
		return err
	}
	mac := hmac.New(sha256.New, key)
	mac.Write(b)
	got := hex.EncodeToString(mac.Sum(nil))
	if !hmac.Equal([]byte(got), []byte(expected)) {
		return fmt.Errorf("signature mismatch for action %q", r.ActionID)
	}
	return nil
}

// FingerprintInput is the complete, order-independent input set of an action.
type FingerprintInput struct {
	ProjectName string
	ActionID    string
	Tool        ToolRef
	Sources     []SourceRef
	Upstreams   []UpstreamRef
}

// ComputeFingerprint hashes the complete input set of an action. It is used
// both by the builder (cache key) and by the verifier (independent
// recomputation). Slices are sorted for determinism.
func ComputeFingerprint(in FingerprintInput) digest.Digest {
	srcs := make([]SourceRef, len(in.Sources))
	copy(srcs, in.Sources)
	sort.Slice(srcs, func(i, j int) bool { return srcs[i].Path < srcs[j].Path })

	ups := make([]UpstreamRef, len(in.Upstreams))
	copy(ups, in.Upstreams)
	sort.Slice(ups, func(i, j int) bool {
		if ups[i].ActionID != ups[j].ActionID {
			return ups[i].ActionID < ups[j].ActionID
		}
		return ups[i].Output < ups[j].Output
	})

	args := append([]string(nil), in.Tool.Args...)

	h := sha256.New()
	write := func(b []byte) {
		var l [8]byte
		for i := 0; i < 8; i++ {
			l[i] = byte(len(b) >> (8 * (7 - i)))
		}
		h.Write(l[:])
		h.Write(b)
	}
	write([]byte(in.ProjectName))
	write([]byte(in.ActionID))
	write([]byte(in.Tool.Path))
	write([]byte(in.Tool.Digest.Hex))
	write([]byte(joinArgs(args)))
	for _, s := range srcs {
		write([]byte(s.Path))
		write([]byte(s.Digest.Hex))
	}
	for _, u := range ups {
		write([]byte(u.ActionID))
		write([]byte(u.Output))
		write([]byte(u.Digest.Hex))
	}
	return digest.Digest{Algo: digest.Algo, Hex: hex.EncodeToString(h.Sum(nil))}
}

func joinArgs(args []string) string {
	out := ""
	for _, a := range args {
		out += fmt.Sprintf("%d:%s\n", len(a), a)
	}
	return out
}
