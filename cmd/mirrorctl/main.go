// Command mirrorctl 是离线镜像安全工具集：
//
//	mirrorctl keygen          生成 ed25519 密钥（签名者/验签器/豁免机构）
//	mirrorctl digest          计算 JSON 文档的 canonical sha256 摘要
//	mirrorctl sign-image      对镜像配置真实签名
//	mirrorctl verify-local    本地测试验签器：验镜像签名并签发验签结果信封
//	mirrorctl grant-exemption 签发“摘要+规则+到期时间”绑定的豁免书
//	mirrorctl gen-examples    生成全部示例输入（密钥、镜像、SBOM、签名、验签结果、豁免）
//
// 不连接任何真实集群或镜像仓库；全部密码学操作本地真实执行。
package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"mirrorsec/internal/cryptox"
	"mirrorsec/internal/localverify"
	"mirrorsec/internal/model"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "keygen":
		err = keygen(os.Args[2:])
	case "digest":
		err = digest(os.Args[2:])
	case "sign-image":
		err = signImage(os.Args[2:])
	case "verify-local":
		err = verifyLocal(os.Args[2:])
	case "grant-exemption":
		err = grantExemption(os.Args[2:])
	case "gen-examples":
		err = genExamples(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "未知子命令: %s\n", os.Args[1])
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "错误: %v\n", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `mirrorctl —— 镜像离线安全工具集

用法: mirrorctl <子命令> [参数]

子命令:
  keygen -out DIR -name NAME
  digest -in FILE
  sign-image -in IMAGE.json -key SIGNER.key -out SIG.json [-digest-out FILE]
  verify-local -image IMAGE.json [-sbom SBOM.json] [-signature SIG.json]
               -verifier-key VERIFIER.key -trusted SIGNER.pub[,SIGNER2.pub...]
               -out VERIFICATION.json
  grant-exemption -digest sha256:... -rule IMG-RUN-ROOT
               -expires RFC3339 [-id ID] [-note TEXT]
               -key EXEMPTION.key -out EXEMPTION.json
  gen-examples -out DIR
`)
}

type flags struct{ m map[string]string }

func parseFlags(args []string, names ...string) (flags, error) {
	f := flags{m: map[string]string{}}
	want := map[string]bool{}
	for _, n := range names {
		want[n] = true
	}
	for i := 0; i < len(args); i++ {
		a := args[i]
		if len(a) < 2 || a[0] != '-' || !want[a[1:]] {
			return f, fmt.Errorf("非法参数: %s", a)
		}
		if i+1 >= len(args) {
			return f, fmt.Errorf("参数 %s 缺少值", a)
		}
		f.m[a[1:]] = args[i+1]
		i++
	}
	return f, nil
}

func (f flags) get(name string) string { return f.m[name] }

func (f flags) require(names ...string) error {
	for _, n := range names {
		if f.m[n] == "" {
			return fmt.Errorf("缺少必填参数 -%s", n)
		}
	}
	return nil
}

func keygen(args []string) error {
	f, err := parseFlags(args, "out", "name")
	if err != nil {
		return err
	}
	if err := f.require("out", "name"); err != nil {
		return err
	}
	pub, priv, err := cryptox.GenerateKeyPair()
	if err != nil {
		return err
	}
	if err := cryptox.WriteKeyPair(f.get("out"), f.get("name"), pub, priv); err != nil {
		return err
	}
	fmt.Printf("已生成 ed25519 密钥: %s.{key,pub}  keyId=%s\n",
		filepath.Join(f.get("out"), f.get("name")), cryptox.KeyID(pub))
	return nil
}

func digest(args []string) error {
	f, err := parseFlags(args, "in")
	if err != nil {
		return err
	}
	if err := f.require("in"); err != nil {
		return err
	}
	raw, err := os.ReadFile(f.get("in"))
	if err != nil {
		return err
	}
	d, err := cryptox.CanonicalDigest(raw)
	if err != nil {
		return err
	}
	fmt.Println(d)
	return nil
}

func signImage(args []string) error {
	f, err := parseFlags(args, "in", "key", "out", "digest-out")
	if err != nil {
		return err
	}
	if err := f.require("in", "key", "out"); err != nil {
		return err
	}
	image, err := os.ReadFile(f.get("in"))
	if err != nil {
		return err
	}
	d, err := cryptox.CanonicalDigest(image)
	if err != nil {
		return err
	}
	priv, err := cryptox.ReadPrivateKey(f.get("key"))
	if err != nil {
		return err
	}
	sig, canonical, err := cryptox.Sign(priv, json.RawMessage(image))
	if err != nil {
		return err
	}
	pub := priv.Public().(ed25519.PublicKey)
	env := model.ImageSignature{
		Algorithm:   "ed25519",
		KeyID:       cryptox.KeyID(pub),
		ImageDigest: cryptox.SHA256Hex(canonical),
		Signature:   cryptox.B64Encode(sig),
	}
	if err := writeJSONIndent(f.get("out"), env); err != nil {
		return err
	}
	fmt.Printf("镜像已签名: digest=%s keyId=%s -> %s\n", d, env.KeyID, f.get("out"))
	if p := f.get("digest-out"); p != "" {
		if err := os.WriteFile(p, []byte(d+"\n"), 0o644); err != nil {
			return err
		}
	}
	return nil
}

func verifyLocal(args []string) error {
	f, err := parseFlags(args, "image", "sbom", "signature", "verifier-key", "trusted", "out")
	if err != nil {
		return err
	}
	if err := f.require("image", "verifier-key", "trusted", "out"); err != nil {
		return err
	}
	image, err := os.ReadFile(f.get("image"))
	if err != nil {
		return err
	}
	var sbom []byte
	if p := f.get("sbom"); p != "" {
		if sbom, err = os.ReadFile(p); err != nil {
			return err
		}
	}
	var imageSig []byte
	if p := f.get("signature"); p != "" {
		if imageSig, err = os.ReadFile(p); err != nil {
			return err
		}
	}
	var signers []ed25519.PublicKey
	for _, p := range splitList(f.get("trusted")) {
		pub, err := cryptox.ReadPublicKey(p)
		if err != nil {
			return fmt.Errorf("受信签名者 %s: %w", p, err)
		}
		signers = append(signers, pub)
	}
	verifierPriv, err := cryptox.ReadPrivateKey(f.get("verifier-key"))
	if err != nil {
		return err
	}
	result, outcome, err := localverify.Run(localverify.Input{
		Image: image, SBOM: sbom, ImageSignature: imageSig, TrustedSigners: signers,
	}, "local-verifier-01", time.Now().UTC())
	if err != nil {
		return err
	}
	env, err := localverify.Seal(result, verifierPriv)
	if err != nil {
		return err
	}
	if err := writeJSONIndent(f.get("out"), env); err != nil {
		return err
	}
	fmt.Printf("本地验签完成: outcome=%s image=%s sbom=%s -> %s\n",
		outcome, result.ImageDigest, orDash(result.SBOMDigest), f.get("out"))
	if outcome == localverify.OutcomeBadSignature || outcome == localverify.OutcomeUnsigned ||
		outcome == localverify.OutcomeUntrusted {
		// 失败如实报告：信封仍写出（记录失败结论），退出码非 0 便于流水线拦截。
		os.Exit(3)
	}
	return nil
}

func grantExemption(args []string) error {
	f, err := parseFlags(args, "digest", "rule", "expires", "id", "note", "key", "out")
	if err != nil {
		return err
	}
	if err := f.require("digest", "rule", "expires", "key", "out"); err != nil {
		return err
	}
	expires, err := time.Parse(time.RFC3339, f.get("expires"))
	if err != nil {
		return fmt.Errorf("解析 -expires（需 RFC3339）: %w", err)
	}
	switch f.get("rule") {
	case model.RuleRoot, model.RulePrivileged, model.RuleBaseAllowlist:
	default:
		return fmt.Errorf("规则 %s 不可豁免（仅 %s/%s/%s 可豁免）",
			f.get("rule"), model.RuleRoot, model.RulePrivileged, model.RuleBaseAllowlist)
	}
	priv, err := cryptox.ReadPrivateKey(f.get("key"))
	if err != nil {
		return err
	}
	id := f.get("id")
	if id == "" {
		id = "exm_" + randomID()
	}
	grant := model.ExemptionGrant{
		ID:          id,
		ImageDigest: f.get("digest"),
		RuleID:      f.get("rule"),
		ExpiresAt:   expires.UTC().Format(time.RFC3339Nano),
		Note:        f.get("note"),
		CreatedAt:   time.Now().UTC().Format(time.RFC3339Nano),
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
	if err := writeJSONIndent(f.get("out"), env); err != nil {
		return err
	}
	fmt.Printf("豁免书已签发: id=%s digest=%s rule=%s expires=%s -> %s\n",
		grant.ID, grant.ImageDigest, grant.RuleID, grant.ExpiresAt, f.get("out"))
	return nil
}

func writeJSONIndent(path string, v any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	return os.WriteFile(path, data, 0o644)
}

func splitList(s string) []string {
	var out []string
	cur := ""
	for _, c := range s {
		if c == ',' {
			if cur != "" {
				out = append(out, cur)
			}
			cur = ""
			continue
		}
		cur += string(c)
	}
	if cur != "" {
		out = append(out, cur)
	}
	return out
}

func orDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}

func randomID() string {
	b := make([]byte, 6)
	if _, err := rand.Read(b); err != nil {
		panic(fmt.Sprintf("crypto/rand 失败: %v", err))
	}
	return hex.EncodeToString(b)
}
