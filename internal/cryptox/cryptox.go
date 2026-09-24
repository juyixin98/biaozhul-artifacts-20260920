// Package cryptox 提供本项目真实使用的密码学原语：
// canonical JSON、SHA-256 摘要、ed25519 签名/验签与 PEM 密钥管理。
//
// 这些操作全部真实执行（不打桩、不模拟）；任何失败都如实向上返回错误，
// 调用方必须按 fail-closed 处理。
package cryptox

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// CanonicalJSON 将任意 JSON 兼容值规范化为稳定字节序列：
// 先解码再以紧凑形式（无空格、键按字典序、关闭 HTML 转义）重新编码。
//
// 这保证签名者与验签者对同一逻辑文档得到相同字节，且与字段书写顺序、
// 空白无关（防止通过插入空格制造“同内容不同摘要”）。
func CanonicalJSON(v any) ([]byte, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return nil, fmt.Errorf("marshal for canonicalization: %w", err)
	}
	var generic any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber() // 大整数不被 float64 破坏精度
	if err := dec.Decode(&generic); err != nil {
		return nil, fmt.Errorf("decode for canonicalization: %w", err)
	}
	return canonicalize(generic)
}

// CanonicalJSONBytes 对已序列化的 JSON 字节做规范化（未知字段也会被覆盖到）。
func CanonicalJSONBytes(raw []byte) ([]byte, error) {
	var generic any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&generic); err != nil {
		return nil, fmt.Errorf("decode for canonicalization: %w", err)
	}
	return canonicalize(generic)
}

func canonicalize(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	// Encode 追加一个换行，去掉它以获得确定结果。
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// SHA256Hex 返回数据的 SHA-256 十六进制摘要（与 OCI 摘要算法一致）。
func SHA256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return "sha256:" + hex.EncodeToString(sum[:])
}

// CanonicalDigest 计算 JSON 文档 canonical 形式的摘要。
func CanonicalDigest(rawJSON []byte) (string, error) {
	can, err := CanonicalJSONBytes(rawJSON)
	if err != nil {
		return "", err
	}
	return SHA256Hex(can), nil
}

// KeyID 取公钥 SHA-256 的前 16 个十六进制字符作为短指纹。
func KeyID(pub ed25519.PublicKey) string {
	sum := sha256.Sum256(pub)
	return hex.EncodeToString(sum[:16])
}

// GenerateKeyPair 真实生成 ed25519 密钥对。
func GenerateKeyPair() (ed25519.PublicKey, ed25519.PrivateKey, error) {
	return ed25519.GenerateKey(rand.Reader)
}

// Sign 对载荷先做 canonical 化再 ed25519 签名，返回 base64。
func Sign(priv ed25519.PrivateKey, payload any) (sig []byte, canonical []byte, err error) {
	canonical, err = CanonicalJSON(payload)
	if err != nil {
		return nil, nil, err
	}
	return ed25519.Sign(priv, canonical), canonical, nil
}

// Verify 对载荷做 canonical 化后验签。
func Verify(pub ed25519.PublicKey, payload any, signature []byte) (canonical []byte, ok bool, err error) {
	canonical, err = CanonicalJSON(payload)
	if err != nil {
		return nil, false, err
	}
	return canonical, ed25519.Verify(pub, canonical, signature), nil
}

// SignRaw 对已规范化的原始字节签名。
func SignRaw(priv ed25519.PrivateKey, message []byte) []byte {
	return ed25519.Sign(priv, message)
}

// B64Encode / B64Decode 标准 base64 包装。
func B64Encode(b []byte) string { return base64.StdEncoding.EncodeToString(b) }

func B64Decode(s string) ([]byte, error) { return base64.StdEncoding.DecodeString(s) }

// MarshalPrivateKey / MarshalPublicKey 以 PKCS#8 PEM 序列化。
func MarshalPrivateKey(priv ed25519.PrivateKey) ([]byte, error) {
	der, err := x509.MarshalPKCS8PrivateKey(priv)
	if err != nil {
		return nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), nil
}

func MarshalPublicKey(pub ed25519.PublicKey) ([]byte, error) {
	der, err := x509.MarshalPKIXPublicKey(pub)
	if err != nil {
		return nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der}), nil
}

// ParsePrivateKeyPEM / ParsePublicKeyPEM 解析 PEM。
func ParsePrivateKeyPEM(data []byte) (ed25519.PrivateKey, error) {
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, errors.New("PEM 解码私钥失败")
	}
	k, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("解析 PKCS#8 私钥: %w", err)
	}
	priv, ok := k.(ed25519.PrivateKey)
	if !ok {
		return nil, errors.New("私钥不是 ed25519")
	}
	return priv, nil
}

func ParsePublicKeyPEM(data []byte) (ed25519.PublicKey, error) {
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, errors.New("PEM 解码公钥失败")
	}
	k, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("解析 PKIX 公钥: %w", err)
	}
	pub, ok := k.(ed25519.PublicKey)
	if !ok {
		return nil, errors.New("公钥不是 ed25519")
	}
	return pub, nil
}

// WriteKeyPair 将私钥/公钥写入目录（0600 / 0644）。
func WriteKeyPair(dir, name string, pub ed25519.PublicKey, priv ed25519.PrivateKey) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	privPEM, err := MarshalPrivateKey(priv)
	if err != nil {
		return err
	}
	pubPEM, err := MarshalPublicKey(pub)
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(dir, name+".key"), privPEM, 0o600); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, name+".pub"), pubPEM, 0o644)
}

// ReadPrivateKey / ReadPublicKey 从文件读取。
func ReadPrivateKey(path string) (ed25519.PrivateKey, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return ParsePrivateKeyPEM(data)
}

func ReadPublicKey(path string) (ed25519.PublicKey, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return ParsePublicKeyPEM(data)
}

// CopyFile 复制文件（用于 gen-examples 等）。
func CopyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o644)
	if err != nil {
		return err
	}
	defer out.Close()
	_, err = io.Copy(out, in)
	return err
}
