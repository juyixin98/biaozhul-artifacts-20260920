// Command genexample 生成 examples/ 下的示例输入：3 个产物、对应真实 Ed25519 签名的
// 测试证据、两个不可变策略版本与密钥。所有签名都在此真实计算，不是占位符。
package main

import (
	"crypto/ed25519"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"atomicpromo/internal/approval"
	"atomicpromo/internal/crypto/sig"
	"atomicpromo/internal/evidence"
	"atomicpromo/internal/policy"
)

func main() {
	dir := flag.String("out", "examples", "output directory")
	flag.Parse()
	must(os.MkdirAll(filepath.Join(*dir, "keys"), 0o755))
	must(os.MkdirAll(filepath.Join(*dir, "artifacts"), 0o755))

	ciPub, ciPriv := loadOrCreateKey(filepath.Join(*dir, "keys", "ci"))
	polPub, polPriv := loadOrCreateKey(filepath.Join(*dir, "keys", "policy-authority"))
	appPub, appPriv := loadOrCreateKey(filepath.Join(*dir, "keys", "approver"))
	_ = appPub

	artifacts := []struct{ name, body string }{
		{"a", "release-candidate A build 4711\n"},
		{"b", "release-candidate B build 4812\n"},
		{"c", "release-candidate C build 4903\n"},
	}
	type dig struct{ name, digest string }
	var digests []dig
	for _, a := range artifacts {
		p := filepath.Join(*dir, "artifacts", a.name+".bin")
		must(os.WriteFile(p, []byte(a.body), 0o644))
		digests = append(digests, dig{a.name, sig.HashBytes([]byte(a.body))})
		fmt.Printf("artifact %s -> %s\n", a.name, sig.HashBytes([]byte(a.body)))
	}

	// 每个 artifact 一份证据 v1（同一 evidence 序列 ev-{name}）
	for _, d := range digests {
		env := evidence.Envelope{
			EvidenceID:     "ev-" + d.name,
			Version:        1,
			ArtifactDigest: d.digest,
			TestsPassed:    true,
			Result: map[string]any{
				"ran_at":   "2026-09-24T00:00:00Z",
				"tests":    map[string]any{"unit": true, "integration": true, "security_scan": d.name != "a"},
				"coverage": map[string]any{"lines": 0.91},
			},
		}
		msg, err := evidence.SigningBytes(env)
		must(err)
		env.SignerKeyID = sig.PublicKeyHex(ciPub)
		env.Signature = sig.Sign(ciPriv, msg)
		writeJSON(filepath.Join(*dir, "evidence-"+d.name+"-v1.json"), env)
	}

	// 证据 a 的 v2：安全测试也通过（用于展示证据版本不可变、新结论必须发新版本）
	envA2 := evidence.Envelope{
		EvidenceID: "ev-a", Version: 2, ArtifactDigest: digests[0].digest, TestsPassed: true,
		Result: map[string]any{
			"ran_at": "2026-09-24T01:00:00Z",
			"tests":  map[string]any{"unit": true, "integration": true, "security_scan": true},
		},
	}
	msg, err := evidence.SigningBytes(envA2)
	must(err)
	envA2.SignerKeyID = sig.PublicKeyHex(ciPub)
	envA2.Signature = sig.Sign(ciPriv, msg)
	writeJSON(filepath.Join(*dir, "evidence-a-v2.json"), envA2)

	mkPolicy := func(ver int64, approval bool, keep int, required []string) {
		body := policy.Body{
			PolicyID: "promotion-policy", Version: ver,
			RequiredTests: required, MinSigners: 1,
			ApprovalRequired: approval, KeepLastN: keep,
		}
		raw, err := body.Canonical()
		must(err)
		sb := policy.SignedBody{Body: body, SignerKeyID: sig.PublicKeyHex(polPub),
			Signature: sig.Sign(polPriv, raw)}
		writeJSON(filepath.Join(*dir, fmt.Sprintf("policy-v%d.json", ver)), sb)
	}
	mkPolicy(1, false, 3, []string{"unit", "integration"})
	mkPolicy(2, true, 2, []string{"unit", "integration", "security_scan"})
	mkPolicy(3, true, 4, []string{"unit", "integration", "security_scan"})

	// 预置一份 gen=0 的审批示例（晋升 A 到 staging，policy v2）；其他代次的审批用 cmd/signapproval 现场签
	d0 := approval.Decision{
		ApprovalID: "apr-a-staging-gen0", PolicyID: "promotion-policy", PolicyVersion: 2,
		Env: "staging", ArtifactDigest: digests[0].digest,
		EvidenceID: "ev-a", EvidenceVersion: 2, ExpectedGen: 0,
		SignerKeyID: sig.PublicKeyHex(appPub),
	}
	d0msg, err := d0.SigningBytes()
	must(err)
	d0.Signature = sig.Sign(appPriv, d0msg)
	writeJSON(filepath.Join(*dir, "approval-a-staging-gen0.json"), d0)

	fmt.Println("keys:")
	fmt.Println("  ci signer        ", sig.PublicKeyHex(ciPub))
	fmt.Println("  policy authority ", sig.PublicKeyHex(polPub))
	fmt.Println("  approver         ", sig.PublicKeyHex(appPub))
}

func loadOrCreateKey(base string) (ed25519.PublicKey, ed25519.PrivateKey) {
	pubPath, keyPath := base+".pub", base+".key"
	if seedHex, err := os.ReadFile(keyPath); err == nil {
		seed, err := hex.DecodeString(strings.TrimSpace(string(seedHex)))
		must(err)
		priv := ed25519.NewKeyFromSeed(seed)
		return priv.Public().(ed25519.PublicKey), priv
	}
	pub, priv, err := sig.GenerateKey()
	must(err)
	must(os.WriteFile(pubPath, []byte(hex.EncodeToString(pub)+"\n"), 0o644))
	must(os.WriteFile(keyPath, []byte(hex.EncodeToString(priv.Seed())+"\n"), 0o600))
	return pub, priv
}

func writeJSON(path string, v any) {
	b, err := json.MarshalIndent(v, "", "  ")
	must(err)
	must(os.WriteFile(path, append(b, '\n'), 0o644))
}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
