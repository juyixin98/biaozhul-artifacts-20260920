// Command verify-standalone verifies a credential's Ed25519 signature
// using ONLY the Go standard library (crypto/ed25519) — none of this
// project's packages — to independently prove the signature produced by
// the server is a standard, real Ed25519 signature.
//
// The public key is passed out-of-band (as in real systems, relying parties
// obtain issuer public keys through trust setup, not from the credential
// itself).
//
//	go run ./examples/cmd/verify-standalone \
//	  -base http://localhost:8090 -cred <credential_id> -pubkey <hex-pubkey>
package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
)

func main() {
	base := flag.String("base", "http://localhost:8090", "server base URL")
	cred := flag.String("cred", "", "credential id")
	pubHex := flag.String("pubkey", "", "hex-encoded ed25519 public key (obtained out-of-band)")
	tamper := flag.Bool("tamper", false, "flip a payload byte (negative control)")
	flag.Parse()
	if *cred == "" || *pubHex == "" {
		fmt.Fprintln(os.Stderr, "usage: verify-standalone -cred <id> -pubkey <hex> [-base url] [-tamper]")
		os.Exit(2)
	}

	pubRaw, err := hex.DecodeString(*pubHex)
	must("decode public key", err)
	if len(pubRaw) != ed25519.PublicKeySize {
		fail("public key must be %d bytes, got %d", ed25519.PublicKeySize, len(pubRaw))
	}

	resp, err := http.Get(fmt.Sprintf("%s/v1/credentials/%s", *base, *cred))
	must("GET credential", err)
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		fail("GET credential -> %d: %s", resp.StatusCode, body)
	}
	var c struct {
		IssuerID  string `json:"issuer_id"`
		Kid       string `json:"kid"`
		Payload   string `json:"payload_b64"`
		Signature string `json:"signature_b64"`
	}
	must("decode credential", json.Unmarshal(body, &c))

	payload, err := base64.StdEncoding.DecodeString(c.Payload)
	must("decode payload", err)
	sig, err := base64.StdEncoding.DecodeString(c.Signature)
	must("decode signature", err)

	if *tamper {
		payload[0] ^= 0xff
	}
	if !ed25519.Verify(ed25519.PublicKey(pubRaw), payload, sig) {
		fmt.Println("VERIFY: INVALID (standard-library Ed25519 rejected the signature)")
		os.Exit(1)
	}
	fmt.Printf("VERIFY: VALID (standard-library Ed25519 accepted; issuer=%s kid=%s payload=%dB)\n",
		c.IssuerID, c.Kid, len(payload))
}

func must(what string, err error) {
	if err != nil {
		fail("%s: %v", what, err)
	}
}
func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "verify-standalone: "+format+"\n", args...)
	os.Exit(1)
}
