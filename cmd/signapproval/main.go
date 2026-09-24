// Command signapproval 用审批人私钥对完整晋升元组做真实 Ed25519 签名，输出可直接 POST 的 JSON。
//
// 示例：
//
//	go run ./cmd/signapproval -key examples/keys/approver.key \
//	  -id apr-b-gen1 -policy promotion-policy -policy-version 2 \
//	  -env staging -digest sha256:... -evidence ev-b -evidence-version 1 -expected-gen 1
package main

import (
	"crypto/ed25519"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"

	"atomicpromo/internal/approval"
	"atomicpromo/internal/crypto/sig"
)

func main() {
	var (
		keyPath         = flag.String("key", "examples/keys/approver.key", "approver private seed (hex file)")
		id              = flag.String("id", "", "approval id")
		policyID        = flag.String("policy", "promotion-policy", "policy id")
		policyVer       = flag.Int64("policy-version", 2, "policy version")
		env             = flag.String("env", "staging", "environment")
		digest          = flag.String("digest", "", "artifact digest sha256:...")
		evidenceID      = flag.String("evidence", "", "evidence id")
		evidenceVersion = flag.Int64("evidence-version", 1, "evidence version")
		expectedGen     = flag.Int64("expected-gen", 0, "environment generation this approval is pinned to")
	)
	flag.Parse()
	if *id == "" || *digest == "" || *evidenceID == "" {
		fmt.Fprintln(os.Stderr, "id, digest and evidence are required")
		os.Exit(2)
	}
	seedHex, err := os.ReadFile(*keyPath)
	must(err)
	seed, err := hex.DecodeString(strings.TrimSpace(string(seedHex)))
	must(err)
	priv := ed25519.NewKeyFromSeed(seed)

	d := approval.Decision{
		ApprovalID: *id, PolicyID: *policyID, PolicyVersion: *policyVer,
		Env: *env, ArtifactDigest: *digest, EvidenceID: *evidenceID,
		EvidenceVersion: *evidenceVersion, ExpectedGen: *expectedGen,
		SignerKeyID: sig.PublicKeyHex(priv.Public().(ed25519.PublicKey)),
	}
	msg, err := d.SigningBytes()
	must(err)
	d.Signature = sig.Sign(priv, msg)
	b, err := json.MarshalIndent(d, "", "  ")
	must(err)
	fmt.Println(string(b))
}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
