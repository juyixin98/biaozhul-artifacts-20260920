// Command genexamples creates a complete, genuinely-signed example bundle in
// examples/generated: two Ed25519 keypairs, allowlisted/non-allowed base
// digests, image configs that pass and fail each rule, real tester
// attestations over the actual bytes, a valid exemption and one
// boundary-expired exemption, plus ready-to-POST request documents.
//
// Run it from the repo root: `go run ./cmd/genexamples`.
package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"mirror-admission/internal/crypto/digest"
	edsig "mirror-admission/internal/crypto/sig"
	"mirror-admission/internal/model"
	"mirror-admission/internal/policy"
	"mirror-admission/internal/verifier"
)

func main() {
	out := flag.String("out", "examples/generated", "output directory")
	flag.Parse()
	if err := generate(*out); err != nil {
		fmt.Fprintln(os.Stderr, "genexamples:", err)
		os.Exit(1)
	}
}

type fixture struct {
	name string
	cfg  map[string]any
	sbom func(base string) map[string]any
}

func generate(out string) error {
	dirs := []string{
		filepath.Join(out, "keys"),
		filepath.Join(out, "configs"),
		filepath.Join(out, "sboms"),
		filepath.Join(out, "attestations"),
		filepath.Join(out, "exemptions"),
		filepath.Join(out, "requests"),
	}
	for _, d := range dirs {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return err
		}
	}

	vPub, vPriv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return err
	}
	ePub, ePriv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return err
	}
	must(edsig.WritePublicKeyPEM(filepath.Join(out, "keys/tester_public.pem"), vPub))
	must(edsig.WritePrivateKeyPEM(filepath.Join(out, "keys/tester_private.pem"), vPriv))
	must(edsig.WritePublicKeyPEM(filepath.Join(out, "keys/exempt_authority_public.pem"), ePub))
	must(edsig.WritePrivateKeyPEM(filepath.Join(out, "keys/exempt_authority_private.pem"), ePriv))
	vID, _ := edsig.KeyID(vPub)
	eID, _ := edsig.KeyID(ePub)

	// Deterministic-looking base digests (still valid sha256 hex shape).
	allowedBase := "sha256:" + repeat('a', 64)
	otherBase := "sha256:" + repeat('b', 64)

	allow := policy.Allowlist{
		Version:           "allowlist-2026.09.24",
		AllowedBaseImages: []string{allowedBase},
	}
	writeJSON(filepath.Join(out, "allowlist.json"), allow)

	// Build scenarios. Times: fixed reference instant so boundary examples
	// are reproducible: expiry == 2026-10-01T00:00:00Z.
	boundary := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)

	type scenario struct {
		name   string
		cfg    map[string]any
		base   string
		result string // tester result
		exempt func(imageD string) (json.RawMessage, bool)
	}
	cfgRoot := oci("root-user", map[string]any{"User": "root", "Privileged": false})
	cfgUID0 := oci("root-uid-zero", map[string]any{"User": "0:0", "Privileged": false})
	cfgNonRoot := oci("non-root", map[string]any{"User": "10001:10001", "Privileged": false})
	cfgPriv := oci("privileged", map[string]any{"User": "10001", "Privileged": true})
	cfgCaps := oci("sysadmin-cap", map[string]any{
		"User": "10001", "Privileged": false,
		"Capabilities": map[string]any{"Add": []string{"CAP_SYS_ADMIN", "CAP_NET_RAW"}},
	})
	cfgMissingUser := oci("missing-user", map[string]any{"Privileged": false})
	cfgNonRootPlain := oci("non-root-plain", map[string]any{"User": "10001", "Privileged": false})

	scenarios := []scenario{
		{name: "01-allow-clean", cfg: cfgNonRoot, base: allowedBase, result: "pass"},
		{name: "02-deny-root", cfg: cfgRoot, base: allowedBase, result: "pass"},
		{name: "03-deny-uid0", cfg: cfgUID0, base: allowedBase, result: "pass"},
		{name: "04-deny-privileged", cfg: cfgPriv, base: allowedBase, result: "pass"},
		{name: "05-deny-sysadmin-cap", cfg: cfgCaps, base: allowedBase, result: "pass"},
		{name: "06-deny-base-not-allowed", cfg: cfgNonRoot, base: otherBase, result: "pass"},
		{name: "07-unknown-missing-user", cfg: cfgMissingUser, base: allowedBase, result: "pass"},
		{name: "08-unknown-no-attestation", cfg: cfgNonRootPlain, base: allowedBase, result: "pass"},
		{name: "09-deny-verifier-fail", cfg: cfgNonRoot, base: allowedBase, result: "fail"},
	}

	for _, sc := range scenarios {
		cfgBytes, _ := json.MarshalIndent(sc.cfg, "", "  ")
		cfgPath := filepath.Join(out, "configs", sc.name+".json")
		must(os.WriteFile(cfgPath, append(cfgBytes, '\n'), 0o644))

		imageD, _, err := digest.ParseOCI(cfgBytes)
		if err != nil {
			return err
		}

		sbomDoc := map[string]any{
			"image_digest":      imageD,
			"base_image_digest": sc.base,
			"distro":            "mirror-offline 1.0",
			"packages":          []any{},
		}
		sbomBytes, _ := json.MarshalIndent(sbomDoc, "", "  ")
		sbomPath := filepath.Join(out, "sboms", sc.name+".json")
		must(os.WriteFile(sbomPath, append(sbomBytes, '\n'), 0o644))

		sbomD, _, err := digest.ParseSBOM(sbomBytes)
		if err != nil {
			return err
		}

		req := map[string]any{
			"image_config":      json.RawMessage(cfgBytes),
			"sbom":              json.RawMessage(sbomBytes),
			"allowlist_version": allow.Version,
		}

		// 08 deliberately carries NO attestation to exercise the
		// missing-evidence => UNKNOWN path.
		if sc.name != "08-unknown-no-attestation" {
			payload := model.VerifierPayload{
				Kind:        verifier.AttestationKind,
				SignerKeyID: vID,
				ImageDigest: imageD,
				SBOMDigest:  sbomD,
				Result:      sc.result,
				Checks:      []string{"root", "privileged", "baseimage", "snapshot"},
				GeneratedAt: time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC).Format(time.RFC3339),
			}
			env, err := verifier.SealAttestation(vPriv, payload)
			if err != nil {
				return err
			}
			attPath := filepath.Join(out, "attestations", sc.name+".json")
			must(os.WriteFile(attPath, append(env, '\n'), 0o644))
			req["attestation"] = json.RawMessage(env)
		}

		writeJSON(filepath.Join(out, "requests", sc.name+".request.json"), req)
	}

	// Signed exemption envelopes are produced below; assemble request
	// variants for 02 (root image) demonstrating waiver semantics.
	cfg02Bytes, _ := os.ReadFile(filepath.Join(out, "configs/02-deny-root.json"))
	sbom02Bytes, _ := os.ReadFile(filepath.Join(out, "sboms/02-deny-root.json"))
	att02Bytes, _ := os.ReadFile(filepath.Join(out, "attestations/02-deny-root.json"))
	imageD02 := digest.SHA256(mustCanon(cfg02Bytes))

	// 10: valid, live, in-scope exemption for no_root_user -> ALLOW (exempt).
	exValid := model.ExemptionPayload{
		Kind: verifier.ExemptionKind, ID: "ex-root-valid-0001",
		SignerKeyID: eID, ImageDigest: imageD02, Rule: "no_root_user",
		NotAfter:  boundary.Add(24 * time.Hour).Format(time.RFC3339),
		Reason:    "break-glass legacy job scheduled for migration",
		GrantedAt: time.Date(2026, 9, 20, 9, 0, 0, 0, time.UTC).Format(time.RFC3339),
	}
	envValid, err := verifier.SealExemption(ePriv, exValid)
	if err != nil {
		return err
	}
	must(os.WriteFile(filepath.Join(out, "exemptions/valid-root-exemption.json"), append(envValid, '\n'), 0o644))
	writeJSON(filepath.Join(out, "requests/10-exempt-root-valid.request.json"), map[string]any{
		"image_config":      json.RawMessage(cfg02Bytes),
		"sbom":              json.RawMessage(sbom02Bytes),
		"attestation":       json.RawMessage(att02Bytes),
		"exemptions":        []any{json.RawMessage(envValid)},
		"allowlist_version": allow.Version,
	})

	// 11: boundary-expired exemption, not_after exactly 2026-10-01T00:00:00Z.
	exExpired := model.ExemptionPayload{
		Kind: verifier.ExemptionKind, ID: "ex-root-boundary-expired",
		SignerKeyID: eID, ImageDigest: imageD02, Rule: "no_root_user",
		NotAfter:  boundary.Format(time.RFC3339),
		Reason:    "expires exactly at the boundary instant",
		GrantedAt: time.Date(2026, 9, 1, 0, 0, 0, 0, time.UTC).Format(time.RFC3339),
	}
	envExpired, err := verifier.SealExemption(ePriv, exExpired)
	if err != nil {
		return err
	}
	must(os.WriteFile(filepath.Join(out, "exemptions/boundary-expired-root.json"), append(envExpired, '\n'), 0o644))
	writeJSON(filepath.Join(out, "requests/11-exempt-boundary-expired.request.json"), map[string]any{
		"image_config":      json.RawMessage(cfg02Bytes),
		"sbom":              json.RawMessage(sbom02Bytes),
		"attestation":       json.RawMessage(att02Bytes),
		"exemptions":        []any{json.RawMessage(envExpired)},
		"allowlist_version": allow.Version,
	})

	// 12: exemption signed by the authority but bound to a different digest.
	exWrong := model.ExemptionPayload{
		Kind: verifier.ExemptionKind, ID: "ex-wrong-digest",
		SignerKeyID: eID, ImageDigest: "sha256:" + repeat('c', 64), Rule: "no_root_user",
		NotAfter:  boundary.Add(48 * time.Hour).Format(time.RFC3339),
		Reason:    "applies to a different image",
		GrantedAt: time.Date(2026, 9, 20, 9, 0, 0, 0, time.UTC).Format(time.RFC3339),
	}
	envWrong, err := verifier.SealExemption(ePriv, exWrong)
	if err != nil {
		return err
	}
	must(os.WriteFile(filepath.Join(out, "exemptions/wrong-digest.json"), append(envWrong, '\n'), 0o644))
	writeJSON(filepath.Join(out, "requests/12-exempt-wrong-digest.request.json"), map[string]any{
		"image_config":      json.RawMessage(cfg02Bytes),
		"sbom":              json.RawMessage(sbom02Bytes),
		"attestation":       json.RawMessage(att02Bytes),
		"exemptions":        []any{json.RawMessage(envWrong)},
		"allowlist_version": allow.Version,
	})

	fmt.Printf("examples generated in %s\n", out)
	fmt.Printf("  tester key id:           %s\n", vID)
	fmt.Printf("  exemption key id:        %s\n", eID)
	fmt.Printf("  allowlist version:       %s\n", allow.Version)
	fmt.Printf("  boundary expiry (UTC):   %s  (equal-time => expired)\n", boundary.Format(time.RFC3339))
	return nil
}

func mustCanon(b []byte) []byte {
	c, err := edsig.CanonicalJSON(b)
	if err != nil {
		panic(err)
	}
	return c
}

func oci(imageName string, cfgSection map[string]any) map[string]any {
	return map[string]any{
		"architecture": "amd64",
		"os":           "linux",
		"created":      "2026-09-20T00:00:00Z",
		"config":       cfgSection,
		"rootfs":       map[string]any{"type": "layers", "diff_ids": []string{}},
		"history":      []any{},
	}
}

func repeat(c byte, n int) string {
	b := make([]byte, n)
	for i := range b {
		b[i] = c
	}
	return string(b)
}

func writeJSON(path string, v any) {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		panic(err)
	}
	if err := os.WriteFile(path, append(b, '\n'), 0o644); err != nil {
		panic(err)
	}
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}
