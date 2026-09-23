// Command verifier is the LOCAL TESTER / POLICY-AUTHORITY tooling that runs
// entirely offline. It never talks to a cluster:
//
//	verifier keygen  -outdir <dir>
//	    generates TWO distinct Ed25519 keypairs (tester + exemption authority)
//	verifier attest  -image <config.json> -sbom <sbom.json> -result pass|fail \
//	                 -key <tester_private.pem> [-checks c1,c2] [-out file]
//	    hashes the actual input bytes and signs the canonical result envelope
//	verifier exempt  -image-digest sha256:... -rule <rule> \
//	                 -not-after 2026-10-01T00:00:00Z -reason ... -id ex-1 \
//	                 -key <exempt_private.pem> [-out file]
//	    signs an exemption bound to one digest, one rule and one deadline
//	verifier keyid   -key <public.pem>
//	    prints the key id (hex sha256 of the public key)
package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"mirror-admission/internal/crypto/digest"
	"mirror-admission/internal/crypto/sig"
	"mirror-admission/internal/model"
	"mirror-admission/internal/verifier"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "verifier:", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		usage()
		return fmt.Errorf("subcommand required")
	}
	switch args[0] {
	case "keygen":
		return keygen(args[1:])
	case "attest":
		return attest(args[1:])
	case "exempt":
		return exempt(args[1:])
	case "keyid":
		return keyid(args[1:])
	case "-h", "--help", "help":
		usage()
		return nil
	default:
		return fmt.Errorf("unknown subcommand %q", args[0])
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `usage: verifier <keygen|attest|exempt|keyid> [flags]
  keygen  -outdir <dir>
  attest  -image cfg.json -sbom sbom.json -result pass|fail -key tester.pem [-checks a,b] [-out env.json]
  exempt  -image-digest sha256:... -rule rule -not-after RFC3339 -id ex-id -reason text -key authority.pem [-out env.json]
  keyid   -key pub.pem
`)
}

func keygen(args []string) error {
	fs := flag.NewFlagSet("keygen", flag.ContinueOnError)
	outdir := fs.String("outdir", ".", "output directory")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if err := os.MkdirAll(*outdir, 0o755); err != nil {
		return err
	}
	if err := genPair(*outdir, "tester"); err != nil {
		return err
	}
	if err := genPair(*outdir, "exempt_authority"); err != nil {
		return err
	}
	return nil
}

func genPair(dir, name string) error {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return err
	}
	pubPath := filepath.Join(dir, name+"_public.pem")
	privPath := filepath.Join(dir, name+"_private.pem")
	if err := sig.WritePublicKeyPEM(pubPath, pub); err != nil {
		return err
	}
	if err := sig.WritePrivateKeyPEM(privPath, priv); err != nil {
		return err
	}
	id, err := sig.KeyID(pub)
	if err != nil {
		return err
	}
	fmt.Printf("generated %s key %s\n  pub:  %s\n  priv: %s\n", name, id, pubPath, privPath)
	return nil
}

func attest(args []string) error {
	fs := flag.NewFlagSet("attest", flag.ContinueOnError)
	imagePath := fs.String("image", "", "path to OCI image config JSON")
	sbomPath := fs.String("sbom", "", "path to SBOM JSON")
	result := fs.String("result", "", "pass|fail")
	keyPath := fs.String("key", "", "tester private PEM")
	checks := fs.String("checks", "root,privileged,baseimage", "comma list of checks")
	out := fs.String("out", "", "output envelope path (stdout if empty)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *imagePath == "" || *sbomPath == "" || *result == "" || *keyPath == "" {
		return fmt.Errorf("attest requires -image, -sbom, -result, -key")
	}
	if *result != "pass" && *result != "fail" {
		return fmt.Errorf("-result must be pass or fail, got %q", *result)
	}
	cfgBytes, err := os.ReadFile(*imagePath)
	if err != nil {
		return err
	}
	sbomBytes, err := os.ReadFile(*sbomPath)
	if err != nil {
		return err
	}
	imageDigest, _, err := digest.ParseOCI(cfgBytes)
	if err != nil {
		return err
	}
	sbomDigest, _, err := digest.ParseSBOM(sbomBytes)
	if err != nil {
		return err
	}
	priv, err := sig.ReadPrivateKeyPEM(*keyPath)
	if err != nil {
		return err
	}
	id, err := sig.KeyID(priv.Public().(ed25519.PublicKey))
	if err != nil {
		return err
	}
	payload := model.VerifierPayload{
		Kind:          verifier.AttestationKind,
		SignerKeyID:   id,
		ImageDigest:   imageDigest,
		SBOMDigest:    sbomDigest,
		Result:        *result,
		Checks:        splitChecks(*checks),
		GeneratedAt:   time.Now().UTC().Format(time.RFC3339Nano),
		VerifierBuild: "cmd/verifier local offline tester",
	}
	env, err := verifier.SealAttestation(priv, payload)
	if err != nil {
		return err
	}
	return emit(env, *out)
}

func splitChecks(s string) []string {
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func exempt(args []string) error {
	fs := flag.NewFlagSet("exempt", flag.ContinueOnError)
	imageDigest := fs.String("image-digest", "", "sha256:... of the ONE image waived")
	rule := fs.String("rule", "", "exact rule id waived")
	notAfter := fs.String("not-after", "", "RFC3339 expiry; boundary time is expired")
	id := fs.String("id", "", "exemption id, e.g. ex-root-2026-09")
	reason := fs.String("reason", "", "human justification recorded on the waiver")
	keyPath := fs.String("key", "", "exemption authority private PEM")
	out := fs.String("out", "", "output envelope path (stdout if empty)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *imageDigest == "" || *rule == "" || *notAfter == "" || *id == "" || *keyPath == "" {
		return fmt.Errorf("exempt requires -image-digest, -rule, -not-after, -id, -key")
	}
	if _, err := time.Parse(time.RFC3339, *notAfter); err != nil {
		return fmt.Errorf("-not-after must be RFC3339: %w", err)
	}
	priv, err := sig.ReadPrivateKeyPEM(*keyPath)
	if err != nil {
		return err
	}
	keyID, err := sig.KeyID(priv.Public().(ed25519.PublicKey))
	if err != nil {
		return err
	}
	payload := model.ExemptionPayload{
		Kind:        verifier.ExemptionKind,
		ID:          *id,
		SignerKeyID: keyID,
		ImageDigest: *imageDigest,
		Rule:        *rule,
		NotAfter:    *notAfter,
		Reason:      *reason,
		GrantedAt:   time.Now().UTC().Format(time.RFC3339Nano),
	}
	env, err := verifier.SealExemption(priv, payload)
	if err != nil {
		return err
	}
	return emit(env, *out)
}

func keyid(args []string) error {
	fs := flag.NewFlagSet("keyid", flag.ContinueOnError)
	keyPath := fs.String("key", "", "public PEM")
	if err := fs.Parse(args); err != nil {
		return err
	}
	pub, err := sig.ReadPublicKeyPEM(*keyPath)
	if err != nil {
		return err
	}
	id, err := sig.KeyID(pub)
	if err != nil {
		return err
	}
	fmt.Println(id)
	return nil
}

func emit(env []byte, path string) error {
	// Indent already applied by Seal*; verify it parses before writing.
	var probe json.RawMessage = env
	if !json.Valid(probe) {
		return fmt.Errorf("internal: produced invalid JSON envelope")
	}
	if path == "" {
		fmt.Println(string(env))
		return nil
	}
	return os.WriteFile(path, append(env, '\n'), 0o644)
}
