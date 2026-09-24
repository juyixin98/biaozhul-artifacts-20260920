// Command sign-attestation creates a fresh, valid attestation envelope using
// the deterministic TEST CI key, hashing a local artifact. It is intended for
// local end-to-end checks against attestation-server — never for real use.
package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"build-attestation/internal/attestation"
	"build-attestation/internal/testkeys"
)

func main() {
	subject := flag.String("subject", "examples/artifact.bin", "artifact file whose sha256 becomes the subject digest")
	repo := flag.String("repo", "https://github.com/example-org/app", "source repository")
	commit := flag.String("commit", "", "source commit (default: deterministic test value sha1:...)")
	paramsFile := flag.String("params", "", "optional JSON file of build parameters")
	out := flag.String("out", "", "output envelope path (default stdout)")
	flag.Parse()

	raw, err := os.ReadFile(*subject)
	if err != nil {
		fatal("read subject: %v", err)
	}
	sum := sha256.Sum256(raw)

	if *commit == "" {
		c := sha256.Sum256([]byte("example commit"))
		*commit = "sha1:" + hex.EncodeToString(c[:])[:40]
	}

	params := map[string]any{"goVersion": "go1.22.2", "platform": "linux/amd64"}
	if *paramsFile != "" {
		praw, err := os.ReadFile(*paramsFile)
		if err != nil {
			fatal("read params: %v", err)
		}
		if err := json.Unmarshal(praw, &params); err != nil {
			fatal("parse params: %v", err)
		}
	}

	current, _, _ := testkeys.CI()
	nonceB := make([]byte, 24)
	if _, err := rand.Read(nonceB); err != nil {
		fatal("nonce: %v", err)
	}

	st := &attestation.Statement{
		Type:      attestation.StatementType,
		BuilderID: "ci.example.net",
		KeyID:     current.ID,
		Source: attestation.Source{
			Repository: *repo,
			Commit:     *commit,
		},
		Subjects: []attestation.Digest{{Alg: "sha256", Value: hex.EncodeToString(sum[:])}},
		Params:   params,
		IssuedAt: time.Now().UTC().Format(time.RFC3339),
		Nonce:    base64.RawURLEncoding.EncodeToString(nonceB),
	}

	env, sErr := attestation.Sign(st, current.Private)
	if sErr != nil {
		fatal("sign: %v", sErr)
	}
	body, err := json.MarshalIndent(env, "", "  ")
	if err != nil {
		fatal("encode: %v", err)
	}
	if *out == "" {
		fmt.Println(string(body))
		return
	}
	if err := os.WriteFile(*out, append(body, '\n'), 0o644); err != nil {
		fatal("write: %v", err)
	}
	fmt.Fprintf(os.Stderr, "wrote %s (key=%s, digest=sha256:%s)\n", *out, current.ID, hex.EncodeToString(sum[:])[:16]+"...")
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "sign-attestation: "+format+"\n", args...)
	os.Exit(1)
}
