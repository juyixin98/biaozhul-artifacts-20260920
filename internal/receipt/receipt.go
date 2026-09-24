// Package receipt 定义晋升/回退成功后签发的收据。
// 收据把“最终环境指针 + 钉死的证据版本 + 策略版本 + 副本校验摘要 + 代次”绑在一起，
// 由服务端 Ed25519 私钥对规范化字节真实签名，可离线验真。
package receipt

import (
	"crypto/ed25519"
	"encoding/hex"
	"os"
	"path/filepath"
	"time"

	"atomicpromo/internal/crypto/canon"
	"atomicpromo/internal/crypto/sig"
)

type Receipt struct {
	Kind             string `json:"kind"` // promote | rollback
	AttemptID        string `json:"attempt_id"`
	Env              string `json:"env"`
	Digest           string `json:"digest"`
	GenBefore        int64  `json:"gen_before"`
	GenAfter         int64  `json:"gen_after"`
	EvidenceID       string `json:"evidence_id,omitempty"`
	EvidenceVersion  int64  `json:"evidence_version,omitempty"`
	PolicyID         string `json:"policy_id,omitempty"`
	PolicyVersion    int64  `json:"policy_version,omitempty"`
	PolicyBodySHA256 string `json:"policy_body_sha256,omitempty"`
	ApprovalID       string `json:"approval_id,omitempty"`
	CopyVerified     string `json:"copy_verified_digest"` // 复制后重新哈希结果，必须等于 digest
	IssuedAt         string `json:"issued_at"`            // RFC3339 UTC
}

func (r Receipt) SigningBytes() ([]byte, error) {
	return canon.Encode(r)
}

// Signed 是收据 + 服务端签名
type Signed struct {
	Receipt   Receipt `json:"receipt"`
	SignerID  string  `json:"signer_key_id"`
	Signature string  `json:"signature"`
}

// Signer 持有服务端收据签名密钥；密钥落盘复用（<dir>/receipt_signing.key，seed 64 字节 hex）
type Signer struct {
	keyID string
	priv  ed25519.PrivateKey
}

func LoadOrCreateSigner(keyDir string) (*Signer, error) {
	if err := os.MkdirAll(keyDir, 0o700); err != nil {
		return nil, err
	}
	path := filepath.Join(keyDir, "receipt_signing.key")
	if raw, err := os.ReadFile(path); err == nil {
		seed, err := hex.DecodeString(string(raw))
		if err != nil || len(seed) != ed25519.SeedSize {
			return nil, os.ErrInvalid
		}
		priv := ed25519.NewKeyFromSeed(seed)
		return &Signer{keyID: sig.PublicKeyHex(priv.Public().(ed25519.PublicKey)), priv: priv}, nil
	}
	pub, priv, err := sig.GenerateKey()
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(path, []byte(hex.EncodeToString(priv.Seed())), 0o600); err != nil {
		return nil, err
	}
	return &Signer{keyID: sig.PublicKeyHex(pub), priv: priv}, nil
}

func (s *Signer) KeyID() string { return s.keyID }

func (s *Signer) Sign(r Receipt) (*Signed, error) {
	if r.IssuedAt == "" {
		r.IssuedAt = time.Now().UTC().Format(time.RFC3339Nano)
	}
	msg, err := r.SigningBytes()
	if err != nil {
		return nil, err
	}
	return &Signed{
		Receipt:   r,
		SignerID:  s.keyID,
		Signature: sig.Sign(s.priv, msg),
	}, nil
}

// VerifySigned 用收据内的 signer 公钥验签（验收脚本/审计使用）
func VerifySigned(sn *Signed) error {
	msg, err := sn.Receipt.SigningBytes()
	if err != nil {
		return err
	}
	return sig.Verify(sn.SignerID, msg, sn.Signature)
}
