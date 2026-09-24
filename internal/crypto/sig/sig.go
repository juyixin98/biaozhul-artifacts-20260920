// Package sig 实现真实的 SHA-256 内容哈希与 Ed25519 签名/验签。
// 不做任何模拟：密钥来自 crypto/ed25519，验签失败即拒绝。
package sig

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
)

const DigestPrefix = "sha256:"

// ErrBadSignature 验签失败
var ErrBadSignature = errors.New("signature verification failed")

// HashReader 流式计算 sha256，返回 "sha256:<hex>"
func HashReader(r io.Reader) (string, error) {
	h := sha256.New()
	if _, err := io.Copy(h, r); err != nil {
		return "", err
	}
	sum := h.Sum(nil)
	return DigestPrefix + hex.EncodeToString(sum), nil
}

// HashBytes 字节内容的 sha256
func HashBytes(b []byte) string {
	sum := sha256.Sum256(b)
	return DigestPrefix + hex.EncodeToString(sum[:])
}

// GenerateKey 生成一对新的 Ed25519 密钥（供测试/CLI 工具使用）
func GenerateKey() (ed25519.PublicKey, ed25519.PrivateKey, error) {
	return ed25519.GenerateKey(rand.Reader)
}

// PublicKeyHex 公钥的 hex 表示，用作 key id
func PublicKeyHex(pub ed25519.PublicKey) string {
	return hex.EncodeToString(pub)
}

// Sign 用私钥对消息签名，返回 base64 标准编码
func Sign(priv ed25519.PrivateKey, msg []byte) string {
	sig := ed25519.Sign(priv, msg)
	return base64.StdEncoding.EncodeToString(sig)
}

// Verify 用 hex 公钥验证 base64 签名
func Verify(pubKeyHex string, msg []byte, sigB64 string) error {
	pub, err := hex.DecodeString(pubKeyHex)
	if err != nil || len(pub) != ed25519.PublicKeySize {
		return fmt.Errorf("%w: invalid signer public key", ErrBadSignature)
	}
	sig, err := base64.StdEncoding.DecodeString(sigB64)
	if err != nil {
		return fmt.Errorf("%w: invalid signature encoding", ErrBadSignature)
	}
	if !ed25519.Verify(ed25519.PublicKey(pub), msg, sig) {
		return ErrBadSignature
	}
	return nil
}
