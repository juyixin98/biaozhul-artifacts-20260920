// attestgen 是测试辅助工具：生成测试密钥、对构建证明签名。
// 仅用于本地测试与演示，私钥文件不得用于生产。
package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"buildattest/internal/attest"
)

// paramsFlag 支持重复的 -param k=v 标志。
type paramsFlag map[string]string

func (p paramsFlag) String() string { return fmt.Sprint(map[string]string(p)) }
func (p paramsFlag) Set(s string) error {
	k, v, ok := strings.Cut(s, "=")
	if !ok || k == "" {
		return fmt.Errorf("-param 需要 k=v 形式")
	}
	p[k] = v
	return nil
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "keygen":
		cmdKeygen(os.Args[2:])
	case "sign":
		cmdSign(os.Args[2:])
	default:
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `用法:
  attestgen keygen -keyid <id> -out <key.json>
  attestgen sign -key <key.json> -digest sha256:<hex> -repo <url> -commit <hex> \
      -builder <id> [-param k=v ...] [-purpose p] [-timestamp RFC3339] [-id attid] [-out att.json]`)
}

func cmdKeygen(args []string) {
	fs := flag.NewFlagSet("keygen", flag.ExitOnError)
	keyID := fs.String("keyid", "", "密钥 ID")
	out := fs.String("out", "", "输出密钥文件（含私钥，仅测试用）")
	_ = fs.Parse(args)
	if *keyID == "" || *out == "" {
		usage()
		os.Exit(2)
	}

	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		fatal("生成密钥失败: %v", err)
	}
	// 注意：该文件包含私钥，仅供本地测试。
	doc := map[string]string{
		"keyId":      *keyID,
		"publicKey":  base64.StdEncoding.EncodeToString(pub),
		"privateKey": base64.StdEncoding.EncodeToString(priv),
		"warning":    "TEST KEY ONLY - do not use in production",
	}
	writeJSONFile(*out, doc)

	pubJSON, _ := json.Marshal(map[string]string{
		"keyId":     *keyID,
		"publicKey": doc["publicKey"],
	})
	fmt.Printf("已写出 %s\n策略 keys 条目（请自行补充 validFrom/validUntil）: %s\n", *out, pubJSON)
}

func cmdSign(args []string) {
	fs := flag.NewFlagSet("sign", flag.ExitOnError)
	keyPath := fs.String("key", "", "keygen 生成的密钥文件")
	digest := fs.String("digest", "", "产物 digest，形如 sha256:<64 hex>")
	repo := fs.String("repo", "", "源码仓库 https URL")
	commit := fs.String("commit", "", "源码 commit（40 位小写十六进制）")
	builder := fs.String("builder", "", "builder 身份")
	purpose := fs.String("purpose", attest.PurposeV1, "用途标识（默认 build-attestation/v1）")
	ts := fs.String("timestamp", "", "RFC3339 时间戳（默认当前时间）")
	id := fs.String("id", "", "attestationId（默认随机生成）")
	out := fs.String("out", "", "输出证明文件（默认标准输出）")
	params := paramsFlag{}
	fs.Var(params, "param", "构建参数 k=v，可重复")
	_ = fs.Parse(args)

	if *keyPath == "" || *digest == "" || *repo == "" || *commit == "" || *builder == "" {
		usage()
		os.Exit(2)
	}

	data, err := os.ReadFile(*keyPath)
	if err != nil {
		fatal("读取密钥文件失败: %v", err)
	}
	var keyDoc struct {
		KeyID      string `json:"keyId"`
		PrivateKey string `json:"privateKey"`
	}
	if err := json.Unmarshal(data, &keyDoc); err != nil {
		fatal("解析密钥文件失败: %v", err)
	}
	privRaw, err := base64.StdEncoding.DecodeString(keyDoc.PrivateKey)
	if err != nil || len(privRaw) != ed25519.PrivateKeySize {
		fatal("密钥文件中的私钥非法")
	}

	timestamp := time.Now().UTC().Truncate(time.Second)
	if *ts != "" {
		timestamp, err = time.Parse(time.RFC3339, *ts)
		if err != nil {
			fatal("timestamp 非法: %v", err)
		}
	}
	attID := *id
	if attID == "" {
		b := make([]byte, 16)
		if _, err := rand.Read(b); err != nil {
			fatal("生成随机 ID 失败: %v", err)
		}
		attID = fmt.Sprintf("att-%x", b)
	}

	a := attest.Attestation{
		AttestationID:  attID,
		Purpose:        *purpose,
		ArtifactDigest: *digest,
		SourceRepo:     *repo,
		SourceCommit:   *commit,
		Builder:        *builder,
		BuildParams:    map[string]string(params),
		Timestamp:      timestamp,
	}
	env, err := attest.SignEnvelope(a, keyDoc.KeyID, ed25519.PrivateKey(privRaw))
	if err != nil {
		fatal("签名失败: %v", err)
	}

	if *out == "" {
		fmt.Println(string(env))
		return
	}
	if err := os.WriteFile(*out, append(env, '\n'), 0o644); err != nil {
		fatal("写出证明失败: %v", err)
	}
	fmt.Printf("已写出 %s\n", *out)
}

func writeJSONFile(path string, v any) {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		fatal("序列化失败: %v", err)
	}
	if err := os.WriteFile(path, append(data, '\n'), 0o600); err != nil {
		fatal("写文件失败: %v", err)
	}
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "错误: "+format+"\n", args...)
	os.Exit(1)
}
