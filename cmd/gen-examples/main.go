// Command gen-examples generates the test trust policy and sample attestation
// envelopes under testdata/ and examples/. All key material is deterministic
// and TEST-ONLY (see internal/testkeys).
package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"build-attestation/internal/attestation"
	"build-attestation/internal/canonical"
	"build-attestation/internal/testkeys"
)

func main() {
	root := flag.String("root", ".", "repository root (writes testdata/ and examples/ under it)")
	flag.Parse()

	td := filepath.Join(*root, "testdata")
	ex := filepath.Join(*root, "examples")
	must(os.MkdirAll(td, 0o755))
	must(os.MkdirAll(ex, 0o755))

	current, old, revoked := testkeys.CI()
	other := testkeys.Other()

	// ---- trust policy with a key-rotation timeline ---------------------
	policyDoc := map[string]any{
		"allowedSourcePrefixes": []string{"https://github.com/example-org/"},
		"builders": []map[string]any{
			{
				"id": "ci.example.net",
				"keys": []map[string]any{
					{
						"id":        current.ID,
						"publicKey": current.PublicHex,
						"notBefore": "2026-01-01T00:00:00Z",
						"notAfter":  "2027-01-01T00:00:00Z",
					},
					{
						"id":        revoked.ID,
						"publicKey": revoked.PublicHex,
						"notBefore": "2026-01-01T00:00:00Z",
						"notAfter":  "2027-01-01T00:00:00Z",
						"revokedAt": "2026-06-01T00:00:00Z",
					},
					{
						"id":        old.ID,
						"publicKey": old.PublicHex,
						"notBefore": "2025-01-01T00:00:00Z",
						"notAfter":  "2026-01-01T00:00:00Z", // rotated away
					},
				},
			},
			{
				"id": "other-builder.example.com",
				"keys": []map[string]any{
					{
						"id":        other.ID,
						"publicKey": other.PublicHex,
						"notBefore": "2026-01-01T00:00:00Z",
						"notAfter":  "2027-01-01T00:00:00Z",
					},
				},
			},
		},
	}
	writeJSONIndent(filepath.Join(td, "policy.json"), policyDoc)

	// ---- a real artifact, hashed with sha256 ----------------------------
	artifact := []byte("example build artifact v1\n")
	must(os.WriteFile(filepath.Join(ex, "artifact.bin"), artifact, 0o644))
	sum := sha256.Sum256(artifact)
	digestHex := hex.EncodeToString(sum[:])

	commitRaw := sha256.Sum256([]byte("example commit"))
	commit := "sha1:" + hex.EncodeToString(commitRaw[:])[:40]

	issued := time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)
	nonce := newNonce()

	base := func() *attestation.Statement {
		return &attestation.Statement{
			Type:      attestation.StatementType,
			BuilderID: "ci.example.net",
			KeyID:     current.ID,
			Source: attestation.Source{
				Repository: "https://github.com/example-org/app",
				Commit:     commit,
			},
			Subjects: []attestation.Digest{{Alg: "sha256", Value: digestHex}},
			Params: map[string]any{
				"goVersion": "go1.22.2",
				"platform":  "linux/amd64",
				"args":      []any{"-trimpath", "-ldflags=-s -w"},
			},
			IssuedAt: issued.Format(time.RFC3339),
			Nonce:    nonce,
		}
	}

	// 1) valid attestation
	validEnv := mustSign(attestation.Sign(base(), current.Private))
	writeEnv(filepath.Join(ex, "valid-envelope.json"), validEnv)

	// 2) digest tampered after signing (flip the first digest nibble)
	tampered := *base()
	tampered.Subjects = []attestation.Digest{{Alg: "sha256", Value: flipHex(digestHex)}}
	tamEnv := mustSign(attestation.Sign(&tampered, current.Private))
	// Now swap the ORIGINAL signature back in: the claim changed but the
	// signature did not, so verification must fail at the crypto step.
	tamEnv.Signature = validEnv.Signature
	writeEnv(filepath.Join(ex, "tampered-digest.json"), tamEnv)

	// 3) signed by the rotated-away (expired) key at the same issuedAt
	oldStmt := base()
	oldStmt.KeyID = old.ID
	oldEnv := mustSign(attestation.Sign(oldStmt, old.Private))
	writeEnv(filepath.Join(ex, "old-key.json"), oldEnv)

	// 4) statement carrying an additional, undeclared field
	extraStmt := map[string]any{
		"_type":     attestation.StatementType,
		"builderId": "ci.example.net",
		"keyId":     current.ID,
		"source":    map[string]any{"repository": "https://github.com/example-org/app", "commit": commit},
		"subjects":  []any{map[string]any{"alg": "sha256", "value": digestHex}},
		"params":    map[string]any{"goVersion": "go1.22.2"},
		"issuedAt":  issued.Format(time.RFC3339),
		"nonce":     newNonce(),
		"debugNote": "I am an extra field that the schema does not define",
	}
	extraPayload := mustCanonical(extraStmt)
	extraSig := ed25519.Sign(current.Private, attestation.SigningInput(extraPayload))
	writeEnv(filepath.Join(ex, "extra-field.json"), &attestation.Envelope{
		PayloadType: attestation.PayloadType,
		Payload:     base64.RawURLEncoding.EncodeToString(extraPayload),
		Signature:   base64.RawURLEncoding.EncodeToString(extraSig),
	})

	// 5) signature created for a different purpose/domain prefix
	crossEnv := mustSign(attestation.SignWithPrefix(base(), current.Private, "SOME_OTHER_PROTOCOL/v1\n"))
	writeEnv(filepath.Join(ex, "cross-purpose.json"), crossEnv)

	// 6) builder is trusted and signature valid, but source is out of policy
	policyStmt := base()
	policyStmt.Source.Repository = "https://github.com/evil-org/app"
	policyStmt.Nonce = newNonce()
	policyEnv := mustSign(attestation.Sign(policyStmt, current.Private))
	writeEnv(filepath.Join(ex, "untrusted-source.json"), policyEnv)

	// 7) non-canonical payload on the wire (insert a space), same signature
	ncRaw, decErr := decodePayload(validEnv.Payload)
	must(decErr)
	ncBytes := []byte(strings.Replace(string(ncRaw), `","`, `" , "`, 1))
	writeEnv(filepath.Join(ex, "noncanonical-wire.json"), &attestation.Envelope{
		PayloadType: attestation.PayloadType,
		Payload:     base64.RawURLEncoding.EncodeToString(ncBytes),
		Signature:   validEnv.Signature,
	})

	// 8) duplicate JSON key inside the payload (crafted raw JSON)
	dupJSON := []byte(`{"_type":"build-attestation/statement/v1","_type":"x"}`)
	writeEnv(filepath.Join(ex, "duplicate-key.json"), &attestation.Envelope{
		PayloadType: attestation.PayloadType,
		Payload:     base64.RawURLEncoding.EncodeToString(dupJSON),
		Signature:   base64.RawURLEncoding.EncodeToString(make([]byte, ed25519.SignatureSize)),
	})

	fmt.Println("generated: testdata/policy.json, examples/*.json, examples/artifact.bin")
}

func flipHex(s string) string {
	if s[0] == '0' {
		return "1" + s[1:]
	}
	return "0" + s[1:]
}

func newNonce() string {
	b := make([]byte, 24)
	_, _ = rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}

func mustCanonical(v map[string]any) []byte {
	raw, err := json.Marshal(v)
	must(err)
	parsed, err := canonical.Parse(raw)
	must(err)
	out, err := canonical.Marshal(parsed)
	must(err)
	return out
}

func decodePayload(s string) ([]byte, error) { return base64.RawURLEncoding.DecodeString(s) }

func writeEnv(path string, env *attestation.Envelope) {
	// Indented envelope for readability; the payload field stays the exact
	// canonical base64url text — only transport whitespace is added.
	writeJSONIndent(path, env)
}

func writeJSONIndent(path string, v any) {
	f, err := os.Create(path)
	must(err)
	defer f.Close()
	enc := json.NewEncoder(f)
	enc.SetIndent("", "  ")
	must(enc.Encode(v))
}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "gen-examples:", err)
		os.Exit(1)
	}
}

// mustSign avoids boxing a nil *VerificationError into a non-nil error
// interface (a Go typed-nil pitfall).
func mustSign(env *attestation.Envelope, e *attestation.VerificationError) *attestation.Envelope {
	if e != nil {
		fmt.Fprintln(os.Stderr, "gen-examples:", e)
		os.Exit(1)
	}
	return env
}
