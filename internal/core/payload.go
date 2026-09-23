package core

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"time"
)

// CanonicalPayload 返回凭证的规范化签名载荷。
// 字段按固定顺序以 "\n" 连接，时间统一为 UTC RFC3339Nano，
// 保证签名与验证两侧逐字节一致。
func CanonicalPayload(c *Credential) []byte {
	return []byte(strings.Join([]string{
		"revcred/v1",
		c.ID,
		c.Issuer,
		c.Subject,
		c.Purpose,
		c.NotBefore.UTC().Format(time.RFC3339Nano),
		c.NotAfter.UTC().Format(time.RFC3339Nano),
		c.ContentDigest,
		c.Kid,
	}, "\n"))
}

// DigestContent 计算内容的 sha256 十六进制摘要。
func DigestContent(content string) string {
	sum := sha256.Sum256([]byte(content))
	return hex.EncodeToString(sum[:])
}

// Sign 用密钥对凭证规范化载荷签名。
func Sign(k *Key, c *Credential) []byte {
	return ed25519.Sign(k.SecretKey, CanonicalPayload(c))
}

// VerifySignature 用公钥校验凭证签名。
func VerifySignature(pub ed25519.PublicKey, c *Credential, sig []byte) bool {
	return ed25519.Verify(pub, CanonicalPayload(c), sig)
}
