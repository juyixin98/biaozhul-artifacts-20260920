// gen-examples：用真实密码学操作生成整套示例输入（不连任何集群/仓库）。
// 生成物包括三类密钥、基础镜像与允许列表、合规/违规/恶意镜像、SBOM、
// 镜像签名、验签结果信封，以及用于边界测试的豁免书。
package main

import (
	"crypto/ed25519"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"mirrorsec/internal/cryptox"
	"mirrorsec/internal/localverify"
	"mirrorsec/internal/model"
)

const baseImageJSON = `{
  "repository": "registry.local/base/distroless",
  "tag": "1.2.3",
  "config": {"user": "nonroot:65532", "privileged": false, "capAdd": []},
  "baseImage": null
}
`

func genExamples(args []string) error {
	f, err := parseFlags(args, "out")
	if err != nil {
		return err
	}
	if err := f.require("out"); err != nil {
		return err
	}
	root := f.get("out")
	keysDir := filepath.Join(root, "keys")
	reqDir := filepath.Join(root, "requests")
	if err := os.MkdirAll(keysDir, 0o755); err != nil {
		return err
	}
	if err := os.MkdirAll(reqDir, 0o755); err != nil {
		return err
	}

	// 1) 三类角色密钥。
	signerPub, signerPriv, err := cryptox.GenerateKeyPair()
	if err != nil {
		return err
	}
	verifierPub, verifierPriv, err := cryptox.GenerateKeyPair()
	if err != nil {
		return err
	}
	exemptPub, exemptPriv, err := cryptox.GenerateKeyPair()
	if err != nil {
		return err
	}
	if err := cryptox.WriteKeyPair(keysDir, "signer", signerPub, signerPriv); err != nil {
		return err
	}
	if err := cryptox.WriteKeyPair(keysDir, "verifier", verifierPub, verifierPriv); err != nil {
		return err
	}
	if err := cryptox.WriteKeyPair(keysDir, "exemption-authority", exemptPub, exemptPriv); err != nil {
		return err
	}
	if err := writeJSONIndent(filepath.Join(keysDir, "trust.json"), map[string]any{
		"verifierKey":           "verifier.pub",
		"exemptionAuthorityKey": "exemption-authority.pub",
		"trustedSignerKeys":     []string{"signer.pub"},
	}); err != nil {
		return err
	}

	// 2) 基础镜像（其摘要进入允许列表；同时复制到 internal/policy 以保持冻结副本一致）。
	basePath := filepath.Join(root, "base-image.json")
	if err := os.WriteFile(basePath, []byte(baseImageJSON), 0o644); err != nil {
		return err
	}
	baseDigest, err := cryptox.CanonicalDigest([]byte(baseImageJSON))
	if err != nil {
		return err
	}
	allow := []model.AllowlistEntry{{
		Repository: "registry.local/base/distroless",
		Digest:     baseDigest,
	}}
	if err := writeJSONIndent(filepath.Join(root, "allowlist.json"), allow); err != nil {
		return err
	}
	if err := writeJSONIndent("internal/policy/allowlist.json", allow); err != nil {
		fmt.Fprintf(os.Stderr, "警告: 无法更新 internal/policy/allowlist.json: %v\n", err)
	}
	if err := os.WriteFile("examples/base-image.json", []byte(baseImageJSON), 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "警告: 无法更新 examples/base-image.json: %v\n", err)
	}

	// 3) 合规镜像：非 root、非特权、基础镜像在允许列表。
	good := model.Image{
		Repository: "registry.local/app/payments",
		Tag:        "v2.1.0",
		Config: model.ContainerConfig{
			User: strptr("appuser:10001"), Privileged: boolptr(false), CapAdd: []string{"NET_BIND_SERVICE"},
		},
		BaseImage: &model.BaseImageRef{
			Repository: "registry.local/base/distroless",
			Digest:     baseDigest,
		},
	}
	goodRaw, _ := jsonMarshal(good)
	goodPath := filepath.Join(reqDir, "good-image.json")
	if err := os.WriteFile(goodPath, append(goodRaw, '\n'), 0o644); err != nil {
		return err
	}

	// 4) SBOM。
	sbom := map[string]any{
		"bomFormat":   "CycloneDX",
		"specVersion": "1.5",
		"component": map[string]any{
			"name":    "payments",
			"version": "2.1.0",
			"purl":    "pkg:oci/payments@v2.1.0?repository_url=registry.local/app",
		},
		"components": []map[string]any{
			{"type": "library", "name": "libcrypto", "version": "3.2.1"},
			{"type": "library", "name": "libc", "version": "2.39"},
		},
	}
	sbomRaw, _ := jsonMarshal(sbom)
	sbomPath := filepath.Join(reqDir, "good-sbom.json")
	if err := os.WriteFile(sbomPath, append(sbomRaw, '\n'), 0o644); err != nil {
		return err
	}

	// 5) 合规镜像签名 + 验签结果。
	goodSig, err := sealImageSignature(goodRaw, signerPriv)
	if err != nil {
		return err
	}
	sigPath := filepath.Join(reqDir, "good-signature.json")
	if err := writeJSONIndent(sigPath, goodSig); err != nil {
		return err
	}
	goodVerification, err := runVerifier(goodRaw, sbomRaw, mustRead(sigPath), verifierPriv, []ed25519.PublicKey{signerPub})
	if err != nil {
		return err
	}
	if err := writeJSONIndent(filepath.Join(reqDir, "good-verification.json"), goodVerification); err != nil {
		return err
	}

	// 6) root 违规镜像及其“有效豁免”（到期时间取生成时刻 +1 小时）。
	rootImg := good
	rootImg.Config.User = strptr("0")
	rootRaw, _ := jsonMarshal(rootImg)
	rootPath := filepath.Join(reqDir, "root-image.json")
	if err := os.WriteFile(rootPath, append(rootRaw, '\n'), 0o644); err != nil {
		return err
	}
	rootDigest, err := cryptox.CanonicalDigest(rootRaw)
	if err != nil {
		return err
	}
	rootSig, err := sealImageSignature(rootRaw, signerPriv)
	if err != nil {
		return err
	}
	if err := writeJSONIndent(filepath.Join(reqDir, "root-signature.json"), rootSig); err != nil {
		return err
	}
	rootSBOMRaw, _ := jsonMarshal(sbom)
	rootVer, err := runVerifier(rootRaw, rootSBOMRaw, mustMarshal(rootSig), verifierPriv, []ed25519.PublicKey{signerPub})
	if err != nil {
		return err
	}
	if err := writeJSONIndent(filepath.Join(reqDir, "root-verification.json"), rootVer); err != nil {
		return err
	}
	future := time.Now().UTC().Add(time.Hour)
	past := time.Now().UTC().Add(-time.Hour)
	if err := writeGrant(filepath.Join(reqDir, "root-exemption-valid.json"), exemptPriv, rootDigest,
		model.RuleRoot, future, "exm-root-valid", "发布窗口内临时 root 豁免"); err != nil {
		return err
	}
	// 边界用：恰好在 1 秒后过期。
	if err := writeGrant(filepath.Join(reqDir, "root-exemption-expire-1s.json"), exemptPriv, rootDigest,
		model.RuleRoot, time.Now().UTC().Add(time.Second), "exm-root-1s", "边界时间测试"); err != nil {
		return err
	}
	// 已过期豁免（必须不生效）。
	if err := writeGrant(filepath.Join(reqDir, "root-exemption-expired.json"), exemptPriv, rootDigest,
		model.RuleRoot, past, "exm-root-expired", "已过期，应被拒绝"); err != nil {
		return err
	}
	// 越界豁免：试图豁免签名规则（签发环节本身允许，策略端必须拒绝采纳）。
	if err := writeGrant(filepath.Join(reqDir, "overreach-exemption.json"), exemptPriv, rootDigest,
		model.RuleSigned, future, "exm-overreach", "越权豁免 IMG-SIGNED"); err != nil {
		return err
	}

	// 7) 标签漂移：good 镜像的旧签名/验签结果 + 改了内容但沿用同一 tag 的新镜像。
	driftImg := good
	driftImg.Config.User = strptr("root") // 内容变了 => 摘要变了，tag 仍是 v2.1.0
	driftRaw, _ := jsonMarshal(driftImg)
	if err := os.WriteFile(filepath.Join(reqDir, "tag-drift-image.json"), append(driftRaw, '\n'), 0o644); err != nil {
		return err
	}
	// 旧验签结果（绑定 good 摘要）随漂移镜像一起提交 => 必须 DIGEST_DRIFT 拒绝。
	if err := cryptox.CopyFile(filepath.Join(reqDir, "good-verification.json"),
		filepath.Join(reqDir, "tag-drift-verification.json")); err != nil {
		return err
	}
	if err := cryptox.CopyFile(filepath.Join(reqDir, "good-signature.json"),
		filepath.Join(reqDir, "tag-drift-signature.json")); err != nil {
		return err
	}

	// 8) 恶意缺字段镜像：故意不带 user / privileged / baseImage。
	malicious := map[string]any{
		"repository": "registry.local/app/malicious",
		"tag":        "latest",
		"config":     map[string]any{"capAdd": []string{"ALL"}},
	}
	malRaw, _ := jsonMarshal(malicious)
	malPath := filepath.Join(reqDir, "malicious-missing-fields.json")
	if err := os.WriteFile(malPath, append(malRaw, '\n'), 0o644); err != nil {
		return err
	}
	malSig, err := sealImageSignature(malRaw, signerPriv)
	if err != nil {
		return err
	}
	malVer, err := runVerifier(malRaw, nil, mustMarshal(malSig), verifierPriv, []ed25519.PublicKey{signerPub})
	if err != nil {
		return err
	}
	if err := writeJSONIndent(filepath.Join(reqDir, "malicious-verification.json"), malVer); err != nil {
		return err
	}

	// 9) 特权镜像（privileged=true）。
	privImg := good
	privImg.Repository = "registry.local/app/privileged"
	privImg.Config.Privileged = boolptr(true)
	privRaw, _ := jsonMarshal(privImg)
	if err := os.WriteFile(filepath.Join(reqDir, "privileged-image.json"), append(privRaw, '\n'), 0o644); err != nil {
		return err
	}
	privSig, err := sealImageSignature(privRaw, signerPriv)
	if err != nil {
		return err
	}
	privVer, err := runVerifier(privRaw, sbomRaw, mustMarshal(privSig), verifierPriv, []ed25519.PublicKey{signerPub})
	if err != nil {
		return err
	}
	if err := writeJSONIndent(filepath.Join(reqDir, "privileged-verification.json"), privVer); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(reqDir, "privileged-sbom.json"), append(sbomRaw, '\n'), 0o644); err != nil {
		return err
	}

	fmt.Printf("示例已生成到 %s/（基础镜像摘要 %s）\n", root, baseDigest)
	fmt.Println("下一步: go run ./cmd/admissiond -trust examples/keys/trust.json")
	return nil
}

func sealImageSignature(image []byte, priv ed25519.PrivateKey) (model.ImageSignature, error) {
	canonical, err := cryptox.CanonicalJSONBytes(image)
	if err != nil {
		return model.ImageSignature{}, err
	}
	pub := priv.Public().(ed25519.PublicKey)
	return model.ImageSignature{
		Algorithm:   "ed25519",
		KeyID:       cryptox.KeyID(pub),
		ImageDigest: cryptox.SHA256Hex(canonical),
		Signature:   cryptox.B64Encode(cryptox.SignRaw(priv, canonical)),
	}, nil
}

func runVerifier(image, sbom, imageSig []byte, verifierPriv ed25519.PrivateKey, signers []ed25519.PublicKey) (model.Envelope, error) {
	result, _, err := localverify.Run(localverify.Input{
		Image: image, SBOM: sbom, ImageSignature: imageSig, TrustedSigners: signers,
	}, "local-verifier-01", time.Now().UTC())
	if err != nil {
		return model.Envelope{}, err
	}
	return localverify.Seal(result, verifierPriv)
}

func writeGrant(path string, priv ed25519.PrivateKey, digest, rule string, expires time.Time, id, note string) error {
	grant := model.ExemptionGrant{
		ID: id, ImageDigest: digest, RuleID: rule,
		ExpiresAt: expires.Format(time.RFC3339Nano), Note: note,
		CreatedAt: time.Now().UTC().Format(time.RFC3339Nano),
	}
	canonical, err := cryptox.CanonicalJSON(grant)
	if err != nil {
		return err
	}
	pub := priv.Public().(ed25519.PublicKey)
	env := model.Envelope{
		Payload:   canonical,
		Algorithm: "ed25519",
		KeyID:     cryptox.KeyID(pub),
		Signature: cryptox.B64Encode(cryptox.SignRaw(priv, canonical)),
	}
	return writeJSONIndent(path, env)
}

func strptr(s string) *string { return &s }
func boolptr(b bool) *bool    { return &b }

func jsonMarshal(v any) ([]byte, error) { return json.Marshal(v) }
func mustRead(p string) []byte {
	b, err := os.ReadFile(p)
	if err != nil {
		panic(err)
	}
	return b
}
func mustMarshal(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return b
}
